import numpy as np
import cv2
import torch
from torch.nn import functional as F
import math
import pytorch3d.transforms as transforms


def split_input(model_input, total_pixels, n_pixels=10000):
    """
    Split the input to fit Cuda memory for large resolution.
    Can decrease the value of n_pixels in case of cuda out of memory error.
    """

    split = []

    for i, indx in enumerate(
        torch.split(torch.arange(total_pixels).cuda(), n_pixels, dim=0)
    ):
        data = model_input.copy()
        data["uv"] = torch.index_select(model_input["uv"], 1, indx)
        split.append(data)
    return split


def merge_output(res, total_pixels, batch_size):
    """Merge the split output."""

    model_outputs = {}
    for entry in res[0]:
        if res[0][entry] is None:
            continue
        if len(res[0][entry].shape) == 1:
            model_outputs[entry] = torch.cat(
                [r[entry].reshape(batch_size, -1, 1) for r in res], 1
            ).reshape(batch_size * total_pixels)
        else:
            model_outputs[entry] = torch.cat(
                [r[entry].reshape(batch_size, -1, r[entry].shape[-1]) for r in res], 1
            ).reshape(batch_size * total_pixels, -1)
    return model_outputs


def get_psnr(img1, img2, normalize_rgb=False):
    if normalize_rgb:  # [-1,1] --> [0,1]
        img1 = (img1 + 1.0) / 2.0
        img2 = (img2 + 1.0) / 2.0

    mse = torch.mean((img1 - img2) ** 2)
    psnr = -10.0 * torch.log(mse) / torch.log(torch.Tensor([10.0]).cuda())

    return psnr


def load_K_Rt_from_P(filename, P=None):
    if P is None:
        lines = open(filename).read().splitlines()
        if len(lines) == 4:
            lines = lines[1:]
        lines = [[x[0], x[1], x[2], x[3]] for x in (x.split(" ") for x in lines)]
        P = np.asarray(lines).astype(np.float32).squeeze()

    out = cv2.decomposeProjectionMatrix(P)
    K = out[0]
    R = out[1]
    t = out[2]

    K = K / K[2, 2]
    intrinsics = np.eye(4)
    intrinsics[:3, :3] = K

    pose = np.eye(4, dtype=np.float32)
    pose[:3, :3] = R.transpose()
    pose[:3, 3] = (t[:3] / t[3])[:, 0]

    return intrinsics, pose


def get_camera_params(uv, pose, intrinsics):
    if pose.shape[1] == 7:  # In case of quaternion vector representation
        cam_loc = pose[:, 4:]
        R = quat_to_rot(pose[:, :4])
        p = torch.eye(4).repeat(pose.shape[0], 1, 1).cuda().float()
        p[:, :3, :3] = R
        p[:, :3, 3] = cam_loc
    else:  # In case of pose matrix representation
        cam_loc = pose[:, :3, 3]
        p = pose

    batch_size, num_samples, _ = uv.shape

    depth = torch.ones((batch_size, num_samples)).cuda()
    x_cam = uv[:, :, 0].view(batch_size, -1)
    y_cam = uv[:, :, 1].view(batch_size, -1)
    z_cam = depth.view(batch_size, -1)

    pixel_points_cam = lift(x_cam, y_cam, z_cam, intrinsics=intrinsics)

    # permute for batch matrix product
    pixel_points_cam = pixel_points_cam.permute(0, 2, 1)

    world_coords = torch.bmm(p, pixel_points_cam).permute(0, 2, 1)[:, :, :3]
    ray_dirs = world_coords - cam_loc[:, None, :]
    ray_dirs = F.normalize(ray_dirs, dim=2)

    return ray_dirs, cam_loc


def get_camera_params_vr_camera(uv, camera_rot):
    long, lat = equirect_to_long_lat(uv)
    ray_dirs = long_lat_to_point(long, lat)
    ray_dirs = coord_z_up_to_y_up_batch(ray_dirs)
    ray_dirs = rotate_camera_to_world(ray_dirs, R=camera_rot)
    ray_dirs = F.normalize(ray_dirs, dim=-1)
    return ray_dirs


def lift(x, y, z, intrinsics):
    # parse intrinsics
    intrinsics = intrinsics.cuda()
    fx = intrinsics[:, 0, 0]
    fy = intrinsics[:, 1, 1]
    cx = intrinsics[:, 0, 2]
    cy = intrinsics[:, 1, 2]
    sk = intrinsics[:, 0, 1]

    x_lift = (
        (
            x
            - cx.unsqueeze(-1)
            + cy.unsqueeze(-1) * sk.unsqueeze(-1) / fy.unsqueeze(-1)
            - sk.unsqueeze(-1) * y / fy.unsqueeze(-1)
        )
        / fx.unsqueeze(-1)
        * z
    )
    y_lift = (y - cy.unsqueeze(-1)) / fy.unsqueeze(-1) * z

    # homogeneous
    return torch.stack((x_lift, y_lift, z, torch.ones_like(z).cuda()), dim=-1)


def quat_to_rot(q):
    batch_size, _ = q.shape
    q = F.normalize(q, dim=1)
    R = torch.ones((batch_size, 3, 3)).cuda()
    qr = q[:, 0]
    qi = q[:, 1]
    qj = q[:, 2]
    qk = q[:, 3]
    R[:, 0, 0] = 1 - 2 * (qj**2 + qk**2)
    R[:, 0, 1] = 2 * (qj * qi - qk * qr)
    R[:, 0, 2] = 2 * (qi * qk + qr * qj)
    R[:, 1, 0] = 2 * (qj * qi + qk * qr)
    R[:, 1, 1] = 1 - 2 * (qi**2 + qk**2)
    R[:, 1, 2] = 2 * (qj * qk - qi * qr)
    R[:, 2, 0] = 2 * (qk * qi - qj * qr)
    R[:, 2, 1] = 2 * (qj * qk + qi * qr)
    R[:, 2, 2] = 1 - 2 * (qi**2 + qj**2)
    return R


def rot_to_quat(R):
    batch_size, _, _ = R.shape
    q = torch.ones((batch_size, 4)).cuda()

    R00 = R[:, 0, 0]
    R01 = R[:, 0, 1]
    R02 = R[:, 0, 2]
    R10 = R[:, 1, 0]
    R11 = R[:, 1, 1]
    R12 = R[:, 1, 2]
    R20 = R[:, 2, 0]
    R21 = R[:, 2, 1]
    R22 = R[:, 2, 2]

    q[:, 0] = torch.sqrt(1.0 + R00 + R11 + R22) / 2
    q[:, 1] = (R21 - R12) / (4 * q[:, 0])
    q[:, 2] = (R02 - R20) / (4 * q[:, 0])
    q[:, 3] = (R10 - R01) / (4 * q[:, 0])
    return q


def get_sphere_intersections(cam_loc, ray_directions, r=1.0):
    # Input: n_rays x 3 ; n_rays x 3
    # Output: n_rays x 1, n_rays x 1 (close and far)

    ray_cam_dot = torch.bmm(
        ray_directions.view(-1, 1, 3), cam_loc.view(-1, 3, 1)
    ).squeeze(-1)
    under_sqrt = ray_cam_dot**2 - (cam_loc.norm(2, 1, keepdim=True) ** 2 - r**2)

    # sanity check
    if (under_sqrt <= 0).sum() > 0:
        print("BOUNDING SPHERE PROBLEM!")
        exit()

    sphere_intersections = (
        torch.sqrt(under_sqrt) * torch.Tensor([-1, 1]).cuda().float() - ray_cam_dot
    )
    sphere_intersections = sphere_intersections.clamp_min(0.0)

    return sphere_intersections


def bilinear_interpolation(xs, ys, dist_map):
    x1 = np.floor(xs).astype(np.int32)
    y1 = np.floor(ys).astype(np.int32)
    x2 = x1 + 1
    y2 = y1 + 1

    dx = np.expand_dims(np.stack([x2 - xs, xs - x1], axis=1), axis=1)
    dy = np.expand_dims(np.stack([y2 - ys, ys - y1], axis=1), axis=2)
    Q = np.stack(
        [dist_map[x1, y1], dist_map[x1, y2], dist_map[x2, y1], dist_map[x2, y2]], axis=1
    ).reshape(-1, 2, 2)
    return np.squeeze(dx @ Q @ dy)  # ((x2 - x1) * (y2 - y1)) = 1


def get_index_outside_of_bbox(samples_uniform, bbox_min, bbox_max):
    samples_uniform_row = samples_uniform[:, 0]
    samples_uniform_col = samples_uniform[:, 1]
    index_outside = np.where(
        (samples_uniform_row < bbox_min[0])
        | (samples_uniform_row > bbox_max[0])
        | (samples_uniform_col < bbox_min[1])
        | (samples_uniform_col > bbox_max[1])
    )[0]
    return index_outside


def weighted_sampling(data, img_size, num_sample, bbox_ratio=0.9):
    """
    More sampling within the bounding box
    """

    # calculate bounding box
    mask = data["object_mask"]
    where = np.asarray(np.where(mask))
    bbox_min = where.min(axis=1)
    bbox_max = where.max(axis=1)

    num_sample_bbox = int(num_sample * bbox_ratio)
    samples_bbox = np.random.rand(num_sample_bbox, 2)
    samples_bbox = samples_bbox * (bbox_max - bbox_min) + bbox_min

    num_sample_uniform = num_sample - num_sample_bbox
    samples_uniform = np.random.rand(num_sample_uniform, 2)
    samples_uniform *= (img_size[0] - 1, img_size[1] - 1)

    # get indices for uniform samples outside of bbox
    index_outside = (
        get_index_outside_of_bbox(samples_uniform, bbox_min, bbox_max) + num_sample_bbox
    )

    indices = np.concatenate([samples_bbox, samples_uniform], axis=0)
    output = {}
    for key, val in data.items():
        if len(val.shape) == 3:
            new_val = np.stack(
                [
                    bilinear_interpolation(indices[:, 0], indices[:, 1], val[:, :, i])
                    for i in range(val.shape[2])
                ],
                axis=-1,
            )
        else:
            new_val = bilinear_interpolation(indices[:, 0], indices[:, 1], val)
        new_val = new_val.reshape(-1, *val.shape[2:])
        output[key] = new_val

    return output, index_outside


def load_pos_init(init_pos_path, indices):
    with open(init_pos_path, "r") as f:
        init = f.readlines()
    init = list(map(lambda x: list(map(lambda y: float(y), x.split(" "))), init))
    init = np.array(init)

    init = init[indices]
    # init = coord_y_up_to_minus_y_up_translate(init)
    init = cm_to_mm(init)

    return init


def load_rotate_init(init_rotate_path, indices):
    with open(init_rotate_path, "r") as f:
        init = f.readlines()
    init = list(map(lambda x: list(map(lambda y: float(y), x.split(" "))), init))
    init = np.array(init)

    init = init[indices]
    init = degree_to_radian(init)

    return init


def coord_z_up_to_y_up_batch(batch):
    return torch.stack([batch[..., 0], batch[..., 2], -batch[..., 1]], dim=-1)


def coord_z_up_to_y_up_translate(T):
    x = T[:, 0]
    y = T[:, 2]
    z = -T[:, 1]
    return np.stack([x, y, z], axis=-1)


def coord_z_up_to_y_up_rotate(R):
    pitch = -R[:, 1]
    yaw = R[:, 2] - 90
    roll = R[:, 0] - 90
    return np.stack([pitch, yaw, roll], axis=-1)


def coord_y_up_to_minus_y_up_translate(T):
    x = T[:, 0]
    y = -T[:, 1]
    z = -T[:, 2]
    return np.stack([x, y, z], axis=-1)


def coord_y_up_to_minus_y_up_rotate(R):
    pitch = R[:, 0]
    yaw = -R[:, 1]
    roll = -R[:, 2]
    return np.stack([pitch, yaw, roll], axis=-1)


def long_lat_to_point(long, lat):
    x = torch.cos(lat) * torch.cos(long)
    y = torch.cos(lat) * torch.sin(long)
    z = torch.sin(lat)
    return torch.stack([x, y, z], dim=-1)


def degree_to_radian(degree):
    return degree * math.pi / 180


def cm_to_mm(x):
    return x * 10


def equirect_to_long_lat(p):
    long = p[..., 0] * math.pi
    lat = p[..., 1] * math.pi / 2
    return long, lat


def get_equi2rect_mapping(idx_equi, height, width, fov):
    """
    idx_equi: (-1 ~ 1, -1 ~ 1)
    idx_rect: (-1 ~ 1, -1 ~ 1)
    """

    # denormalize (-1, 1) => (0, height or width)
    idx_equi = denormalize_points(idx_equi, height, width)

    # equirectangular coordinates to spherical coordinates.
    x = (idx_equi[..., 0] - width / 2) / (width / 2)
    y = (idx_equi[..., 1] - height / 2) / (height / 2)
    long = (x - 1) * math.pi / 2
    lat = y * math.pi / 2

    # shperical coordinates to normalized device coordinates.
    x = np.cos(lat) * np.cos(long)
    y = np.cos(lat) * np.sin(long)
    z = np.sin(lat)
    x = x / (z + 1e-8)
    y = y / (z + 1e-8)


def denormalize_points(points, height, width):
    # (-1~1,-1~1) => (0~width, 0~height)
    x = (points[..., 0] + 1) / 2 * width
    y = (points[..., 1] + 1) / 2 * height
    points = np.stack([x, y], axis=-1)
    return points


# TODO batch version
def coord_world_to_camera(points, R, T):
    """
    points: numpy array (N,3)
    R: numpy array (3,3)
    T: numpy array (3,)
    """
    points = points - T
    points = np.matmul(points, R)
    return points


# TODO batch version
def coord_camera_to_world(points, R, T):
    """
    points: numpy array (N,3)
    R: numpy array (3,3)
    T: numpy array (3,)
    """
    points = np.matmul(points, R.transpose(0, 1))
    points = points + T
    return points


# TODO merge with numpy version
def rotate_camera_to_world(points, R):
    """
    points: tensor (B,N,3)
    R: tensor (B,3,3)
    T: tensor (B,3)
    """
    R = transforms.euler_angles_to_matrix(R, "ZXY")
    points = torch.bmm(R, points.transpose(1, 2)).transpose(1, 2)
    return points


def read_image(input_path, method="opencv"):
    if method == "opencv":
        img = cv2.imread(input_path, cv2.IMREAD_ANYCOLOR | cv2.IMREAD_ANYDEPTH)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    else:
        raise TypeError("unsupported method.")
    return img


def clip_and_convert_rgb_to_srgb(img: np.ndarray):
    img = np.clip(img, 0, 1)
    # convert colour to sRGB
    img = np.where(
        img <= 0.0031308, 12.92 * img, 1.055 * np.power(img, 1 / 2.4) - 0.055
    )
    return img


@torch.jit.script
def inverse_3x3_batch(mat):
    assert mat.shape[1:] == (3, 3)
    
    det = mat[:, 0, 0] * (mat[:, 1, 1] * mat[:, 2, 2] - mat[:, 1, 2] * mat[:, 2, 1]) - \
          mat[:, 0, 1] * (mat[:, 1, 0] * mat[:, 2, 2] - mat[:, 1, 2] * mat[:, 2, 0]) + \
          mat[:, 0, 2] * (mat[:, 1, 0] * mat[:, 2, 1] - mat[:, 1, 1] * mat[:, 2, 0])
    
    singular_mask = det == 0
    if singular_mask.any():
        print("Warning: Singular matrices found.")
    
    adj = torch.zeros_like(mat)
    
    adj[:, 0, 0] = mat[:, 1, 1] * mat[:, 2, 2] - mat[:, 1, 2] * mat[:, 2, 1]
    adj[:, 0, 1] = mat[:, 0, 2] * mat[:, 2, 1] - mat[:, 0, 1] * mat[:, 2, 2]
    adj[:, 0, 2] = mat[:, 0, 1] * mat[:, 1, 2] - mat[:, 0, 2] * mat[:, 1, 1]
    
    adj[:, 1, 0] = mat[:, 1, 2] * mat[:, 2, 0] - mat[:, 1, 0] * mat[:, 2, 2]
    adj[:, 1, 1] = mat[:, 0, 0] * mat[:, 2, 2] - mat[:, 0, 2] * mat[:, 2, 0]
    adj[:, 1, 2] = mat[:, 1, 0] * mat[:, 0, 2] - mat[:, 0, 0] * mat[:, 1, 2]
    
    adj[:, 2, 0] = mat[:, 1, 0] * mat[:, 2, 1] - mat[:, 1, 1] * mat[:, 2, 0]
    adj[:, 2, 1] = mat[:, 2, 0] * mat[:, 0, 1] - mat[:, 0, 0] * mat[:, 2, 1]
    adj[:, 2, 2] = mat[:, 0, 0] * mat[:, 1, 1] - mat[:, 1, 0] * mat[:, 0, 1]
    
    inv_matrices = adj / det.unsqueeze(-1).unsqueeze(-1)
    
    inv_matrices[singular_mask] = float('nan')
    
    return inv_matrices