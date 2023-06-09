import os
import sys
project_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.append(project_dir)
os.chdir(project_dir)
from datetime import datetime
from pyhocon import ConfigFactory
import numpy as np
import argparse
import GPUtil
import torch
import utils.general as utils
from model.sample import Sampler
from model.network import gradient
from scipy.spatial import cKDTree
from utils.plots import plot_surface, plot_cuts
from tqdm import tqdm
import matplotlib.pyplot as plt 
import seaborn as sns 
sns.set_style("darkgrid")

class ReconstructionRunner:

    def run(self):

        print("running")

        self.data = self.data.cuda()
        self.data.requires_grad_()

        if self.eval:

            print("evaluating epoch: {0}".format(self.startepoch))
            my_path = os.path.join(self.cur_exp_dir, 'evaluation', str(self.startepoch))

            utils.mkdir_ifnotexists(os.path.join(self.cur_exp_dir, 'evaluation'))
            utils.mkdir_ifnotexists(my_path)
            self.plot_shapes(epoch=self.startepoch, path=my_path, with_cuts=True)
            return

        print("training")

        loss_dict = {"loss": []}
        if (self.conf.get_string('network.loss.type') == "IGR"):
            loss_dict["manifold_loss"] = []
            loss_dict["eikonal_loss"] = []

        elif (self.conf.get_string('network.loss.type') == "phase"):
            loss_dict["reconstruction_loss"] = []
            loss_dict["regularization_loss"] = []

        for epoch in tqdm(range(self.startepoch, self.nepochs + 1)):
            print(f"epoch = {epoch}")

            indices = torch.tensor(np.random.choice(self.data.shape[0], self.points_batch, False))

            cur_data = self.data[indices]

            mnfld_pnts = cur_data[:, :self.d_in]
            mnfld_sigma = self.local_sigma[indices]

            if epoch % self.conf.get_int('train.checkpoint_frequency') == 0:
                print('saving checkpoint: ', epoch)
                self.save_checkpoints(epoch)
                print('plot validation epoch: ', epoch)
                self.plot_shapes(epoch)

            # change back to train mode
            self.network.train()
            self.adjust_learning_rate(epoch)

            # forward pass
            if (self.conf.get_string('train.encoding') == "FF"):
                mnfld_pnts = torch.fft.fft(mnfld_pnts).real
                mnfld_pred = torch.fft.ifft(self.network(mnfld_pnts)).real

            else:
                mnfld_pred = self.network(mnfld_pnts)

            # compute grad
            mnfld_grad = gradient(mnfld_pnts, mnfld_pred)

            if (self.conf.get_string('network.loss.type') == "IGR"):
                nonmnfld_pnts = self.sampler.get_points(mnfld_pnts.unsqueeze(0), mnfld_sigma.unsqueeze(0)).squeeze()
                if (self.conf.get_string('train.encoding') == "FF"):
                    nonmnfld_pnts = torch.fft.fft(nonmnfld_pnts).real
                    nonmnfld_pred = torch.fft.ifft(self.network(nonmnfld_pnts)).real

                else:
                    nonmnfld_pred = self.network(nonmnfld_pnts)
                nonmnfld_grad = gradient(nonmnfld_pnts, nonmnfld_pred)
                
                # manifold loss
                mnfld_loss = (mnfld_pred.abs()).mean()

                # eikonal loss
                grad_loss = ((nonmnfld_grad.norm(2, dim=-1) - 1) ** 2).mean()

                loss = mnfld_loss + self.grad_lambda * grad_loss

                # normals loss
                if self.with_normals:
                    normals = cur_data[:, -self.d_in:]
                    normals_loss = ((mnfld_grad - normals).abs()).norm(2, dim=1).mean()
                    loss = loss + self.normals_lambda * normals_loss
                else:
                    normals_loss = torch.zeros(1)
                
                loss_dict["loss"].append(loss.item())
                loss_dict["manifold_loss"].append(mnfld_loss.item())
                loss_dict["eikonal_loss"].append(grad_loss.item())

            elif (self.conf.get_string('network.loss.type') == "phase"):
                u = mnfld_pred # [B, 1]

                num_samples = self.conf.get_int('network.loss.sample_count')
                sigma = self.conf.get_float('network.loss.sampling_sigma')
                local_x = (mnfld_pnts.unsqueeze(dim=1).repeat(1, num_samples, 1) + (torch.randn((mnfld_pnts.shape[0], num_samples, mnfld_pnts.shape[1])) * sigma).cuda())
                if (self.conf.get_string('train.encoding') == "FF"):
                    local_x = torch.fft.fft(local_x).real
                    local_u = torch.fft.ifft(self.network(local_x)).real.squeeze()

                else:
                    local_u = self.network(local_x).squeeze()

                # reconstruction term 1 (L)
                L = torch.mean(torch.abs(torch.mean(local_u, dim=-1)), dim=-1) # [1,]

                # reconstruction term 2 
                grad_u = mnfld_grad # [B, 3]
                norm_grad_u = mnfld_grad.norm(2, dim=-1) # [B, 1]
                epsilon = self.conf.get_float('network.loss.epsilon')
                W = utils.potential(u) # [B, ]
                integral_term = torch.mean((epsilon * torch.pow(norm_grad_u, 2)) + W) # [1, ]

                reconstruction_loss = (self.conf.get_float('network.loss.lambda') * L) + integral_term

                # regularization term
                w = (-1) * np.sqrt(self.conf.get_float('network.loss.epsilon')) * torch.log(torch.abs(1 - torch.sum(u.squeeze()))) * torch.sign(u)
                grad_w = gradient(mnfld_pnts, w)
                norm_grad_w = grad_w.norm(2, dim=-1)

                if self.with_normals:
                    normals = cur_data[:, -self.d_in:]
                    regularization_loss = torch.mean(torch.pow((normals - grad_w).norm(2, dim=-1), 2))
                
                else:
                    regularization_loss = torch.mean(torch.pow(1 - norm_grad_w, 2))

                loss = reconstruction_loss + (self.conf.get_float('network.loss.mu') * regularization_loss)

                loss_dict["loss"].append(loss.item())
                loss_dict["reconstruction_loss"].append(reconstruction_loss.item())
                loss_dict["regularization_loss"].append(regularization_loss.item())

            # back propagation

            self.optimizer.zero_grad()

            loss.backward()

            self.optimizer.step()

            if epoch % self.conf.get_int('train.status_frequency') == 0:
                if (self.conf.get_string('network.loss.type') == "IGR"):
                    print('Train Epoch: [{}/{} ({:.0f}%)]\tTrain Loss: {:.6f}\tManifold loss: {:.6f}'
                        '\tGrad loss: {:.6f}\tNormals Loss: {:.6f}'.format(
                        epoch, self.nepochs, 100. * epoch / self.nepochs,
                        loss.item(), mnfld_loss.item(), grad_loss.item(), normals_loss.item()))
                    loss
                       
                elif (self.conf.get_string('network.loss.type') == "phase"):
                    print('Train Epoch: [{}/{} ({:.0f}%)]\tTrain Loss: {:.6f}\tReconstruction loss: {:.6f}'
                                '\tRegularization loss: {:.6f}'.format(
                                epoch, self.nepochs, 100. * epoch / self.nepochs,
                                loss.item(), reconstruction_loss.item(), regularization_loss.item()))
        
        for key in loss_dict.keys():
            plt.figure(figsize=(10, 8))
            plt.plot(loss_dict[key], label=key)
            plt.legend()
            plt.tight_layout()
            plt.savefig(self.cur_exp_dir + f"/{key}.png")
            plt.close()


    def plot_shapes(self, epoch, path=None, with_cuts=False):
        # plot network validation shapes
        with torch.no_grad():

            self.network.eval()

            if not path:
                path = self.plots_dir

            indices = torch.tensor(np.random.choice(self.data.shape[0], self.points_batch, False))

            pnts = self.data[indices, :3]

            plot_surface(with_points=True,
                         points=pnts,
                         decoder=self.network,
                         path=path,
                         epoch=epoch,
                         shapename=self.expname,
                         **self.conf.get_config('plot'))

            if with_cuts:
                plot_cuts(points=pnts,
                          decoder=self.network,
                          path=path,
                          epoch=epoch,
                          near_zero=False)

    def __init__(self, **kwargs):

        self.home_dir = os.path.abspath(os.pardir)

        # config setting

        if type(kwargs['conf']) == str:
            self.conf_filename = './reconstruction/' + kwargs['conf']
            self.conf = ConfigFactory.parse_file(self.conf_filename)
        else:
            self.conf = kwargs['conf']

        self.expname = kwargs['expname']

        # GPU settings

        self.GPU_INDEX = kwargs['gpu_index']

        if not self.GPU_INDEX == 'ignore':
            os.environ["CUDA_VISIBLE_DEVICES"] = '{0}'.format(self.GPU_INDEX)

        self.num_of_gpus = torch.cuda.device_count()

        self.eval = kwargs['eval']

        # settings for loading an existing experiment

        if (kwargs['is_continue'] or self.eval) and kwargs['timestamp'] == 'latest':
            if os.path.exists(os.path.join(self.home_dir, 'exps', self.expname)):
                timestamps = os.listdir(os.path.join(self.home_dir, 'exps', self.expname))
                if (len(timestamps)) == 0:
                    is_continue = False
                    timestamp = None
                else:
                    timestamp = sorted(timestamps)[-1]
                    is_continue = True
            else:
                is_continue = False
                timestamp = None
        else:
            timestamp = kwargs['timestamp']
            is_continue = kwargs['is_continue'] or self.eval

        self.exps_folder_name = 'exps'

        utils.mkdir_ifnotexists(utils.concat_home_dir(os.path.join(self.home_dir, self.exps_folder_name)))

        self.input_file = self.conf.get_string('train.input_path')
        # self.data = utils.load_point_cloud_by_file_extension(self.input_file)
        self.data = utils.load_point_cloud_files_from_folder(self.input_file)

        sigma_set = []
        ptree = cKDTree(self.data)

        for p in np.array_split(self.data, 100, axis=0):
            d = ptree.query(p, 50 + 1)
            sigma_set.append(d[0][:, -1])

        sigmas = np.concatenate(sigma_set)
        self.local_sigma = torch.from_numpy(sigmas).float().cuda()

        self.expdir = utils.concat_home_dir(os.path.join(self.home_dir, self.exps_folder_name, self.expname))
        utils.mkdir_ifnotexists(self.expdir)

        if is_continue:
            self.timestamp = timestamp
        else:
            self.timestamp = '{:%Y_%m_%d_%H_%M_%S}'.format(datetime.now())

        self.cur_exp_dir = os.path.join(self.expdir, self.timestamp)
        utils.mkdir_ifnotexists(self.cur_exp_dir)

        self.plots_dir = os.path.join(self.cur_exp_dir, 'plots')
        utils.mkdir_ifnotexists(self.plots_dir)

        self.checkpoints_path = os.path.join(self.cur_exp_dir, 'checkpoints')
        utils.mkdir_ifnotexists(self.checkpoints_path)

        self.checkpoints_path = os.path.join(self.cur_exp_dir, 'checkpoints')
        utils.mkdir_ifnotexists(self.checkpoints_path)

        self.model_params_subdir = "ModelParameters"
        self.optimizer_params_subdir = "OptimizerParameters"

        utils.mkdir_ifnotexists(os.path.join(self.checkpoints_path, self.model_params_subdir))
        utils.mkdir_ifnotexists(os.path.join(self.checkpoints_path, self.optimizer_params_subdir))

        self.nepochs = kwargs['nepochs']

        self.points_batch = kwargs['points_batch']

        self.global_sigma = self.conf.get_float('network.sampler.properties.global_sigma')
        self.sampler = Sampler.get_sampler(self.conf.get_string('network.sampler.sampler_type'))(self.global_sigma,
                                                                                                 self.local_sigma)
        self.grad_lambda = self.conf.get_float('network.loss.lambda')
        self.normals_lambda = self.conf.get_float('network.loss.normals_lambda')

        # use normals if data has  normals and normals_lambda is positive
        self.with_normals = self.normals_lambda > 0 and self.data.shape[-1] >= 6

        self.d_in = self.conf.get_int('train.d_in')

        self.network = utils.get_class(self.conf.get_string('train.network_class'))(d_in=self.d_in,
                                                                                    **self.conf.get_config(
                                                                                        'network.inputs'))

        if torch.cuda.is_available():
            self.network.cuda()

        self.lr_schedules = self.get_learning_rate_schedules(self.conf.get_list('train.learning_rate_schedule'))
        self.weight_decay = self.conf.get_float('train.weight_decay')

        self.startepoch = 0

        self.optimizer = torch.optim.Adam(
            [
                {
                    "params": self.network.parameters(),
                    "lr": self.lr_schedules[0].get_learning_rate(0),
                    "weight_decay": self.weight_decay
                },
            ])

        # if continue load checkpoints

        if is_continue:
            old_checkpnts_dir = os.path.join(self.expdir, timestamp, 'checkpoints')

            saved_model_state = torch.load(
                os.path.join(old_checkpnts_dir, 'ModelParameters', str(kwargs['checkpoint']) + ".pth"))
            self.network.load_state_dict(saved_model_state["model_state_dict"])

            data = torch.load(
                os.path.join(old_checkpnts_dir, 'OptimizerParameters', str(kwargs['checkpoint']) + ".pth"))
            self.optimizer.load_state_dict(data["optimizer_state_dict"])
            self.startepoch = saved_model_state['epoch']

    def get_learning_rate_schedules(self, schedule_specs):

        schedules = []

        for schedule_specs in schedule_specs:

            if schedule_specs["Type"] == "Step":
                schedules.append(
                    utils.StepLearningRateSchedule(
                        schedule_specs["Initial"],
                        schedule_specs["Interval"],
                        schedule_specs["Factor"],
                    )
                )

            else:
                raise Exception(
                    'no known learning rate schedule of type "{}"'.format(
                        schedule_specs["Type"]
                    )
                )

        return schedules

    def adjust_learning_rate(self, epoch):
        for i, param_group in enumerate(self.optimizer.param_groups):
            param_group["lr"] = self.lr_schedules[i].get_learning_rate(epoch)
            print(f"Epoch {epoch}: lr = {param_group['lr']}")

    def save_checkpoints(self, epoch):

        torch.save(
            {"epoch": epoch, "model_state_dict": self.network.state_dict()},
            os.path.join(self.checkpoints_path, self.model_params_subdir, str(epoch) + ".pth"))
        torch.save(
            {"epoch": epoch, "model_state_dict": self.network.state_dict()},
            os.path.join(self.checkpoints_path, self.model_params_subdir, "latest.pth"))

        torch.save(
            {"epoch": epoch, "optimizer_state_dict": self.optimizer.state_dict()},
            os.path.join(self.checkpoints_path, self.optimizer_params_subdir, str(epoch) + ".pth"))
        torch.save(
            {"epoch": epoch, "optimizer_state_dict": self.optimizer.state_dict()},
            os.path.join(self.checkpoints_path, self.optimizer_params_subdir, "latest.pth"))


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--points_batch', type=int, default=16384, help='point batch size')
    parser.add_argument('--nepoch', type=int, default=100000, help='number of epochs to train for')
    parser.add_argument('--conf', type=str, default='setup.conf')
    parser.add_argument('--expname', type=str, default='single_shape')
    parser.add_argument('--gpu', type=str, default='2', help='GPU to use [default: GPU auto]')
    parser.add_argument('--is_continue', default=False, action="store_true", help='continue')
    parser.add_argument('--timestamp', default='latest', type=str)
    parser.add_argument('--checkpoint', default='latest', type=str)
    parser.add_argument('--eval', default=False, action="store_true")

    args = parser.parse_args()

    if args.gpu == "auto":
        deviceIDs = GPUtil.getAvailable(order='memory', limit=1, maxLoad=0.5, maxMemory=0.5, includeNan=False, excludeID=[],
                                    excludeUUID=[])
        gpu = deviceIDs[0]
    else:
        gpu = args.gpu

    trainrunner = ReconstructionRunner(
            conf=args.conf,
            points_batch=args.points_batch,
            nepochs=args.nepoch,
            expname=args.expname,
            gpu_index=gpu,
            is_continue=args.is_continue,
            timestamp=args.timestamp,
            checkpoint=args.checkpoint,
            eval=args.eval
    )

    trainrunner.run()
