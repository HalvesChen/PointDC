import torch
import os
from glob import glob
import numpy as np
from torch.utils.data import Dataset
import MinkowskiEngine as ME
import random
import open3d as o3d
from lib.aug_tools import rota_coords, scale_coords, trans_coords
from lib.helper_ply import read_ply as read_ply
from os.path import join
from tqdm import tqdm

class S3DISdistill(Dataset):
    def __init__(self, args, areas=['Area_1', 'Area_2', 'Area_3', 'Area_4', 'Area_6']):
        self.args = args
        self.name = []
        self.mode = 'disitll'
        self.clip_bound = 4 # 4m
        self.file = []
        self.label_to_names = {0: 'ceiling',
                               1: 'floor',
                               2: 'wall',
                               3: 'beam',
                               4: 'column',
                               5: 'window',
                               6: 'door',
                               7: 'table',
                               8: 'chair',
                               9: 'sofa',
                               10: 'bookcase',
                               11: 'board',
                               12: 'clutter'}

        ''' Reading Data'''
        folders_bar = tqdm(sorted(glob(join(self.args.data_path, 'input_spfeats', '*.pt'))))
        for file in folders_bar:
            folders_bar.set_description('PreLoad')
            ptname = os.path.basename(file)
            if ptname[0:6] in areas:
                self.name.append(ptname[0:-14])
                self.file.append(torch.load(file))

        '''Initial Augmentations'''
        self.trans_coords = trans_coords(shift_ratio=50) ### 50%
        self.rota_coords = rota_coords(rotation_bound = ((-np.pi/32, np.pi/32), (-np.pi/32, np.pi/32), (-np.pi, np.pi)))
        self.scale_coords = scale_coords(scale_bound = (0.9, 1.1))


    def augs(self, coords, feats):
        coords = self.rota_coords(coords)
        coords = self.trans_coords(coords)
        coords = self.scale_coords(coords)
        return coords, feats

    def augment_coords_to_feats(self, coords, colors, labels=None):
        coords_center = coords.mean(0, keepdims=True)
        coords_center[0, 2] = 0
        norm_coords = (coords - coords_center)

        feats = norm_coords
        feats = np.concatenate((colors, feats), axis=-1)
        return norm_coords, feats, labels

    def clip(self, coords, center=None):
        bound_min = np.min(coords, 0).astype(float)
        bound_max = np.max(coords, 0).astype(float)
        bound_size = bound_max - bound_min
        if center is None:
            center = bound_min + bound_size * 0.5
        lim = self.clip_bound

        if isinstance(self.clip_bound, (int, float)):
            if bound_size.max() < self.clip_bound:
                return None
            else:
                clip_inds = ((coords[:, 0] >= (-lim + center[0])) & (coords[:, 0] < (lim + center[0])) & \
                             (coords[:, 1] >= (-lim + center[1])) & (coords[:, 1] < (lim + center[1])) & \
                             (coords[:, 2] >= (-lim + center[2])) & (coords[:, 2] < (lim + center[2])))
                return clip_inds

    def voxelize(self, coords, feats, labels):
        assert coords.shape[1] == 3 and coords.shape[0] == feats.shape[0] and coords.shape[0]
        clip_inds = self.clip(coords)
        if clip_inds is not None:
            coords, feats = coords[clip_inds], feats[clip_inds]
            if labels is not None:
                labels = labels[clip_inds]

        scale = 1 / self.args.voxel_size
        coords = np.floor(coords * scale)
        coords, feats, labels, unique_map, inverse_map = \
                    ME.utils.sparse_quantize(np.ascontiguousarray(coords), feats, \
                    labels=labels, ignore_label=-1, return_index=True, return_inverse=True)
        return coords.numpy(), feats, labels, unique_map, clip_inds, inverse_map.numpy()

    def __len__(self):
        return len(self.file)

    def __getitem__(self, index):
        dinofeats = self.file[index]
        data = read_ply(self.args.sp_path+'processed/'+self.name[index]+'.ply')
        coords, colors, labels = np.vstack((data['x'], data['y'], data['z'])).T, np.vstack((data['red'], data['green'], data['blue'])).T, data['class']
        colors = colors.astype(np.float32)
        coords = coords.astype(np.float32)
        coords -= coords.mean(0)
        labels = np.ones(coords.shape[0])

        coords, colors, labels, unique_map, clip_inds, inverse_map = self.voxelize(coords, colors, labels)
        coords = coords.astype(np.float32)

        region_file = self.args.sp_path + 'initial_superpoints_rebuild/' + self.name[index] + '_rebuild_superpoint.npy'
        region      = np.load(region_file).astype(np.int64)
        dinofeats   = dinofeats[region+1]

        '''Clip if Scene includes much Points'''
        if clip_inds is not None:
            region    = region[clip_inds]
            dinofeats = dinofeats[clip_inds]
        assert region.shape[0]==inverse_map.shape[0], "check pd file and sp file"
        region    = region[unique_map]
        dinofeats = dinofeats[unique_map]

        coords, colors = self.augs(coords, colors)

        coords, feats, labels = self.augment_coords_to_feats(coords, colors/255-0.5, labels)
        labels[labels == self.args.ignore_label] = -1

        normals = np.zeros_like(coords)
        scene_name = self.name[index]

        return coords, feats, dinofeats, normals, labels, inverse_map, region, index, scene_name

class S3DIScluster(Dataset):
    def __init__(self, args, areas=['Area_1', 'Area_2', 'Area_3', 'Area_4', 'Area_6']):
        self.args = args
        self.name = []
        self.file = []
        self.label_to_names = {0: 'ceiling',
                               1: 'floor',
                               2: 'wall',
                               3: 'beam',
                               4: 'column',
                               5: 'window',
                               6: 'door',
                               7: 'table',
                               8: 'chair',
                               9: 'sofa',
                               10: 'bookcase',
                               11: 'board',
                               12: 'clutter'}

        ''' Reading Data'''
        folders = sorted(glob(join(self.args.data_path, 'processed', '*.ply')))
        for _, file in enumerate(folders):
            plyname = os.path.basename(file)
            if plyname[0:6] in areas:
                self.name.append(plyname[0:-4])
                self.file.append(file)

        '''Initial Augmentations'''
        self.trans_coords = trans_coords(shift_ratio=50) ### 50%
        self.rota_coords = rota_coords(rotation_bound = ((-np.pi/32, np.pi/32), (-np.pi/32, np.pi/32), (-np.pi, np.pi)))
        self.scale_coords = scale_coords(scale_bound = (0.9, 1.1))

    def augs(self, coords, feats):
        coords = self.rota_coords(coords)
        coords = self.trans_coords(coords)
        coords = self.scale_coords(coords)
        return coords, feats

    def augment_coords_to_feats(self, coords, colors, labels=None):
        coords_center = coords.mean(0, keepdims=True)
        coords_center[0, 2] = 0
        norm_coords = (coords - coords_center)

        feats = norm_coords
        feats = np.concatenate((colors, feats), axis=-1)
        return norm_coords, feats, labels

    def voxelize(self, coords, feats, labels):
        assert coords.shape[1] == 3 and coords.shape[0] == feats.shape[0] and coords.shape[0]

        scale = 1 / self.args.voxel_size
        coords = np.floor(coords * scale)
        coords, feats, labels, unique_map, inverse_map = ME.utils.sparse_quantize(np.ascontiguousarray(coords), feats, labels=labels, ignore_label=-1, return_index=True, return_inverse=True)
        return coords.numpy(), feats, labels, unique_map, inverse_map.numpy()

    def __len__(self):
        return len(self.file)

    def __getitem__(self, index):
        scene_name = self.name[index] 
        data = read_ply(self.file[index])
        coords, colors, labels = np.vstack((data['x'], data['y'], data['z'])).T, \
                                    np.vstack((data['red'], data['green'], data['blue'])).T, data['class']
        colors = colors.astype(np.float32)
        coords = coords.astype(np.float32)
        coords -= coords.mean(0)
        
        # load region 
        region_file = self.args.sp_path + 'initial_superpoints/' + scene_name + '_superpoint.npy'
        region = np.load(region_file)
        
        coords, colors, _, unique_map, inverse_map = self.voxelize(coords, colors, labels)
        coords = coords.astype(np.float32)

        inds = np.arange(coords.shape[0])

        coords, feats, labels = self.augment_coords_to_feats(coords, colors/255-0.5, labels)
        labels[labels == self.args.ignore_label] = -1

        normals = np.zeros_like(coords)  
        pseudo  = -np.ones_like(labels).astype(np.long)
        
        region[labels==-1] = -1
        for q in np.unique(region):
            mask = q == region
            if mask.sum() < self.args.drop_threshold and q != -1:
                region[mask] = -1
        valid_region = region[region != -1]
        unique_vals = np.unique(valid_region)
        unique_vals.sort()
        valid_region = np.searchsorted(unique_vals, valid_region)
        region[region != -1] = valid_region

        return coords, feats, normals, labels, inverse_map, pseudo, inds, region, index, scene_name

class S3DIStrain(Dataset):
    def __init__(self, args, areas=['Area_1', 'Area_2', 'Area_3', 'Area_4', 'Area_6']):
        self.args = args
        self.name = []
        self.mode = 'train'
        self.clip_bound = 4 # 4m
        self.file = []
        self.label_to_names = {0: 'ceiling',
                               1: 'floor',
                               2: 'wall',
                               3: 'beam',
                               4: 'column',
                               5: 'window',
                               6: 'door',
                               7: 'table',
                               8: 'chair',
                               9: 'sofa',
                               10: 'bookcase',
                               11: 'board',
                               12: 'clutter'}

        ''' Reading Data'''
        folders = sorted(glob(join(self.args.data_path, 'processed', '*.ply')))
        for _, file in enumerate(folders):
            plyname = os.path.basename(file)
            if plyname[0:6] in areas:
                self.name.append(plyname[0:-4])
                self.file.append(file)

        '''Initial Augmentations'''
        self.trans_coords = trans_coords(shift_ratio=50) ### 50%
        self.rota_coords = rota_coords(rotation_bound = ((-np.pi/32, np.pi/32), (-np.pi/32, np.pi/32), (-np.pi, np.pi)))
        self.scale_coords = scale_coords(scale_bound = (0.9, 1.1))

    def augs(self, coords, feats):
        coords = self.rota_coords(coords)
        coords = self.trans_coords(coords)
        coords = self.scale_coords(coords)
        return coords, feats

    def augment_coords_to_feats(self, coords, colors, labels=None):
        coords_center = coords.mean(0, keepdims=True)
        coords_center[0, 2] = 0
        norm_coords = (coords - coords_center)

        feats = norm_coords
        feats = np.concatenate((colors, feats), axis=-1)
        return norm_coords, feats, labels

    def clip(self, coords, center=None):
        bound_min = np.min(coords, 0).astype(float)
        bound_max = np.max(coords, 0).astype(float)
        bound_size = bound_max - bound_min
        if center is None:
            center = bound_min + bound_size * 0.5
        lim = self.clip_bound

        if isinstance(self.clip_bound, (int, float)):
            if bound_size.max() < self.clip_bound:
                return None
            else:
                clip_inds = ((coords[:, 0] >= (-lim + center[0])) & (coords[:, 0] < (lim + center[0])) & \
                             (coords[:, 1] >= (-lim + center[1])) & (coords[:, 1] < (lim + center[1])) & \
                             (coords[:, 2] >= (-lim + center[2])) & (coords[:, 2] < (lim + center[2])))
                return clip_inds

    def voxelize(self, coords, feats, labels):
        assert coords.shape[1] == 3 and coords.shape[0] == feats.shape[0] and coords.shape[0]
        clip_inds = self.clip(coords)
        if clip_inds is not None:
            coords, feats = coords[clip_inds], feats[clip_inds]
            if labels is not None:
                labels = labels[clip_inds]

        scale = 1 / self.args.voxel_size
        coords = np.floor(coords * scale)
        coords, feats, labels, unique_map, inverse_map = ME.utils.sparse_quantize(np.ascontiguousarray(coords), feats, labels=labels, ignore_label=-1, return_index=True, return_inverse=True)
        return coords.numpy(), feats, labels, unique_map, clip_inds, inverse_map.numpy()

    def __len__(self):
        return len(self.file)

    def __getitem__(self, index):
        scene_name = self.name[index] 
        data = read_ply(self.file[index])
        coords, colors, labels = np.vstack((data['x'], data['y'], data['z'])).T, np.vstack((data['red'], data['green'], data['blue'])).T, data['class']
        colors = colors.astype(np.float32)
        coords = coords.astype(np.float32)
        coords -= coords.mean(0)
        
        # load region 
        region_file = self.args.sp_path + 'initial_superpoints/' + scene_name + '_superpoint.npy'
        region = np.load(region_file)
        
        coords, colors, _, unique_map, clip_inds, inverse_map = self.voxelize(coords, colors, labels)
        coords = coords.astype(np.float32)

        '''Clip if Scene includes much Points'''
        if clip_inds is not None:
            region = region[clip_inds]
            labels = labels[clip_inds]
        # region = region[unique_map]
        
        coords, colors = self.augs(coords, colors)
        inds = np.arange(coords.shape[0])

        coords, feats, labels = self.augment_coords_to_feats(coords, colors/255-0.5, labels)
        labels[labels == self.args.ignore_label] = -1

        '''mode must be cluster or train'''
        if self.mode == 'cluster':
            normals = np.zeros_like(coords)  
            pseudo  = -np.ones_like(labels).astype(np.long)
            
            region[labels==-1] = -1
            for q in np.unique(region):
                mask = q == region
                if mask.sum() < self.args.drop_threshold and q != -1:
                    region[mask] = -1
            valid_region = region[region != -1]
            unique_vals = np.unique(valid_region)
            unique_vals.sort()
            valid_region = np.searchsorted(unique_vals, valid_region)
            region[region != -1] = valid_region
            
        elif self.mode == 'train':
            normals    = np.zeros_like(coords)
            scene_name = self.name[index]
            file_path  = self.args.pseudo_path + '/' + scene_name + '.npy'
            pseudo     = np.array(np.load(file_path), dtype=np.long)

            if clip_inds is not None:
                pseudo = pseudo[clip_inds]

            pseudo[labels == -1] = -1
            pseudo = pseudo[unique_map]

        return coords, feats, normals, labels, inverse_map, pseudo, inds, region, index, scene_name

class S3DIStest(Dataset):
    def __init__(self, args, areas=['Area_5']):
        self.args = args
        self.name = []
        self.file = []
        self.label_to_names = {0: 'ceiling',
                               1: 'floor',
                               2: 'wall',
                               3: 'beam',
                               4: 'column',
                               5: 'window',
                               6: 'door',
                               7: 'table',
                               8: 'chair',
                               9: 'sofa',
                               10: 'bookcase',
                               11: 'board',
                               12: 'clutter'}

        ''' Reading Data'''
        folders = sorted(glob(join(self.args.data_path, 'processed', '*.ply')))
        for _, file in enumerate(folders):
            plyname = os.path.basename(file)
            if plyname[0:6] in areas:
                self.name.append(plyname[0:-4])
                self.file.append(file)

    def augment_coords_to_feats(self, coords, colors, labels=None):
        coords_center = coords.mean(0, keepdims=True)
        coords_center[0, 2] = 0
        norm_coords = (coords - coords_center)#*self.voxel_size

        feats = norm_coords
        feats = np.concatenate((colors, feats), axis=-1)
        return norm_coords, feats, labels

    def voxelize(self, coords, feats, labels):
        assert coords.shape[1] == 3 and coords.shape[0] == feats.shape[0] and coords.shape[0]
        scale = 1 / self.args.voxel_size
        coords = np.floor(coords * scale)
        coords, feats, labels, unique_map, inverse_map = ME.utils.sparse_quantize(np.ascontiguousarray(coords), feats, labels=labels, ignore_label=-1, return_index=True, return_inverse=True)
        return coords.numpy(), feats, labels, unique_map, inverse_map.numpy()

    def __len__(self):
        return len(self.file)

    def __getitem__(self, index):
        data = read_ply(self.file[index])
        coords, colors, labels = np.vstack((data['x'], data['y'], data['z'])).T, np.vstack((data['red'], data['green'], data['blue'])).T, data['class']
        colors = colors.astype(np.float32)
        coords = coords.astype(np.float32)
        coords -= coords.mean(0)

        coords, colors, _, unique_map, inverse_map = self.voxelize(coords, colors, labels)
        coords = coords.astype(np.float32)
        region_file = self.args.sp_path + 'initial_superpoints/' + self.name[index] + '_superpoint.npy'
        region = np.load(region_file)

        labels[labels == self.args.ignore_label] = -1
        region[labels == -1] = -1
        region = region[unique_map]

        valid_region = region[region != -1]
        unique_vals = np.unique(valid_region)
        unique_vals.sort()
        valid_region = np.searchsorted(unique_vals, valid_region)

        region[region != -1] = valid_region

        coords, feats, labels = self.augment_coords_to_feats(coords, colors/255-0.5, labels)
        return coords, feats, inverse_map, np.ascontiguousarray(labels), index, region

class cfl_collate_fn_distill:

    def __call__(self, list_data):
        coords, feats, dinofeats, normals, labels, inverse_map, region, index, scene_name = list(zip(*list_data))
        coords_batch, feats_batch, dinofeats_batch, normal_batch, labels_batch, inverse_batch, \
                                            region_batch, scene_batch = [], [], [], [], [], [], [], []
        accm_num = 0
        for batch_id, _ in enumerate(coords):
            num_points = coords[batch_id].shape[0]
            coords_batch.append(torch.cat((torch.ones(num_points, 1).int() * batch_id, torch.from_numpy(coords[batch_id]).int()), 1))
            feats_batch.append(torch.from_numpy(feats[batch_id]))
            dinofeats_batch.append(torch.from_numpy(dinofeats[batch_id]))
            normal_batch.append(torch.from_numpy(normals[batch_id]))
            labels_batch.append(torch.from_numpy(labels[batch_id]).int())
            inverse_batch.append(torch.from_numpy(inverse_map[batch_id]))
            region_batch.append(torch.from_numpy(region[batch_id])[:,None])
            scene_batch.append(scene_name[batch_id])
            accm_num += coords[batch_id].shape[0]

        # Concatenate all lists
        coords_batch    = torch.cat(coords_batch, 0).float()#.int()
        feats_batch     = torch.cat(feats_batch, 0).float()
        dinofeats_batch = torch.cat(dinofeats_batch, 0).float()
        normal_batch    = torch.cat(normal_batch, 0).float()
        labels_batch    = torch.cat(labels_batch, 0).float()
        inverse_batch   = torch.cat(inverse_batch, 0).int()
        region_batch    = torch.cat(region_batch, 0)

        return coords_batch, feats_batch, dinofeats_batch, normal_batch, labels_batch, inverse_batch, region_batch, index, scene_batch

class cfl_collate_fn:

    def __call__(self, list_data):
        coords, feats, normals, labels, inverse_map, pseudo, inds, region, index, scenenames = list(zip(*list_data))
        coords_batch, feats_batch, normal_batch, labels_batch, inverse_batch, pseudo_batch, inds_batch = [], [], [], [], [], [], []
        region_batch = []
        accm_num = 0
        for batch_id, _ in enumerate(coords):
            num_points = coords[batch_id].shape[0]
            coords_batch.append(torch.cat((torch.ones(num_points, 1).int() * batch_id, torch.from_numpy(coords[batch_id]).int()), 1))
            feats_batch.append(torch.from_numpy(feats[batch_id]))
            normal_batch.append(torch.from_numpy(normals[batch_id]))
            labels_batch.append(torch.from_numpy(labels[batch_id].copy()).int())
            inverse_batch.append(torch.from_numpy(inverse_map[batch_id]))
            pseudo_batch.append(torch.from_numpy(pseudo[batch_id]))
            inds_batch.append(torch.from_numpy(inds[batch_id] + accm_num).int())
            region_batch.append(torch.from_numpy(region[batch_id])[:,None])
            accm_num += coords[batch_id].shape[0]

        # Concatenate all lists
        coords_batch = torch.cat(coords_batch, 0).float()#.int()
        feats_batch = torch.cat(feats_batch, 0).float()
        normal_batch = torch.cat(normal_batch, 0).float()
        labels_batch = torch.cat(labels_batch, 0).float()
        inverse_batch = torch.cat(inverse_batch, 0).int()
        pseudo_batch = torch.cat(pseudo_batch, -1)
        inds_batch = torch.cat(inds_batch, 0)
        region_batch = torch.cat(region_batch, 0).long()

        return coords_batch, feats_batch, normal_batch, labels_batch, inverse_batch, pseudo_batch, inds_batch, region_batch, index, scenenames


class cfl_collate_fn_test:

    def __call__(self, list_data):
        coords, feats, inverse_map, labels, index, region= list(zip(*list_data))
        coords_batch, feats_batch, inverse_batch, labels_batch = [], [], [], []
        region_batch = []
        for batch_id, _ in enumerate(coords):
            num_points = coords[batch_id].shape[0]
            coords_batch.append(torch.cat((torch.ones(num_points, 1).int() * batch_id, torch.from_numpy(coords[batch_id]).int()), 1))
            feats_batch.append(torch.from_numpy(feats[batch_id]))
            inverse_batch.append(torch.from_numpy(inverse_map[batch_id]))
            labels_batch.append(torch.from_numpy(labels[batch_id]).int())
            region_batch.append(torch.from_numpy(region[batch_id])[:,None])
        #
        # Concatenate all lists
        coords_batch = torch.cat(coords_batch, 0).float()
        feats_batch = torch.cat(feats_batch, 0).float()
        inverse_batch = torch.cat(inverse_batch, 0).int()
        labels_batch = torch.cat(labels_batch, 0).int()
        region_batch = torch.cat(region_batch, 0)

        return coords_batch, feats_batch, inverse_batch, labels_batch, index, region_batch