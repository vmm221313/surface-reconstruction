import os
import sys
import glob
import trimesh
import numpy as np

import torch


def potential(x):
    return (torch.pow(x, 2) - 2*torch.abs(x) + 1)


def mkdir_ifnotexists(directory):
    if not os.path.exists(directory):
        os.mkdir(directory)


def as_mesh(scene_or_mesh):
    """
    Convert a possible scene to a mesh.

    If conversion occurs, the returned mesh has only vertex and face data.
    """
    if isinstance(scene_or_mesh, trimesh.Scene):
        if len(scene_or_mesh.geometry) == 0:
            mesh = None  # empty scene
        else:
            # we lose texture information here
            mesh = trimesh.util.concatenate(
                tuple(trimesh.Trimesh(vertices=g.vertices, faces=g.faces)
                    for g in scene_or_mesh.geometry.values()))
    else:
        assert(isinstance(scene_or_mesh, trimesh.Trimesh))
        mesh = scene_or_mesh
    return mesh


def concat_home_dir(path):
    return os.path.join(os.environ['HOME'],'data',path)


def get_class(kls):
    parts = kls.split('.')
    module = ".".join(parts[:-1])
    m = __import__(module)
    for comp in parts[1:]:
        m = getattr(m, comp)
    return m


def to_cuda(torch_obj):
    if torch.cuda.is_available():
        return torch_obj.cuda()
    else:
        return torch_obj


def load_point_cloud_files_from_folder(dir_path):
    # TODO: take this from config
    # ext = "xyz" 
    ext = "xyz" 

    tensor_list = []
    for file in glob.glob(f"{dir_path}/*.{ext}"):
        print(f"loading file {file}")
        if (ext == "npy"):
            tensor_list.append(torch.from_numpy(np.load(file)).float())
        else:
            tensor_list.append(torch.tensor(trimesh.load(file, ext).vertices).float())
        print(tensor_list[-1].shape)

    point_set = torch.vstack(tensor_list)
    point_set = point_set - torch.mean(point_set)
    print(f"data shape = {point_set.shape}")

    return point_set


def load_point_cloud_by_file_extension(file_name):

    ext = file_name.split('.')[-1]

    if ext == "npz" or ext == "npy":
        point_set = torch.tensor(np.load(file_name)).float()
    else:
        point_set = torch.tensor(trimesh.load(file_name, ext).vertices).float()

    return point_set


class LearningRateSchedule:
    def get_learning_rate(self, epoch):
        pass


class StepLearningRateSchedule(LearningRateSchedule):
    def __init__(self, initial, interval, factor):
        self.initial = initial
        self.interval = interval
        self.factor = factor

    def get_learning_rate(self, epoch):
        return np.maximum(self.initial * (self.factor ** (epoch // self.interval)), 5.0e-6)