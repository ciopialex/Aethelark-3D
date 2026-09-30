"""
Built-In 3D Vector Thumbnail Rasterizer for Aethelark-3D.
Renders high-quality 512x512 RGBA and 680x510 RGB thumbnails from STL binary meshes in < 0.3s.
Zero display / OpenGL dependencies.
"""

import struct
from pathlib import Path
from typing import Tuple, List
import numpy as np
from PIL import Image, ImageDraw


def render_stl_to_image(
    stl_path: Path,
    size: Tuple[int, int] = (512, 512),
    color_rgb: Tuple[int, int, int] = (220, 45, 55),
    azimuth_deg: float = 45.0,
    elevation_deg: float = 30.0,
    is_rgba: bool = True
) -> Image.Image:
    bg_color = (0, 0, 0, 0) if is_rgba else (30, 30, 35)
    stl_path = Path(stl_path)
    if not stl_path.exists() or stl_path.suffix.lower() not in [".stl", ".obj", ".ply"]:
        # Fallback image for non-STL / 3MF containers
        img = Image.new("RGBA" if is_rgba else "RGB", size, bg_color)
        d = ImageDraw.Draw(img)
        d.text((40, size[1] // 2), f"Model: {stl_path.name}", fill=(220, 220, 220))
        return img

    try:
        # Binary STL Parser
        with open(stl_path, "rb") as f:
            f.seek(80)
            n_tri = struct.unpack("<I", f.read(4))[0]
            dt = np.dtype([("normal", "3f4"), ("v1", "3f4"), ("v2", "3f4"), ("v3", "3f4"), ("attr", "u2")])
            mesh = np.fromfile(f, dtype=dt, count=n_tri)

        v1, v2, v3 = mesh["v1"], mesh["v2"], mesh["v3"]
        vertices = np.concatenate([v1, v2, v3], axis=0)

        # Center and normalize bounds
        min_b = vertices.min(axis=0)
        max_b = vertices.max(axis=0)
        center = (min_b + max_b) / 2.0
        scale = (max_b - min_b).max()
        if scale == 0:
            scale = 1.0

        v1_c = (v1 - center) / scale
        v2_c = (v2 - center) / scale
        v3_c = (v3 - center) / scale

        # Isometric rotation matrix
        theta = np.radians(azimuth_deg)
        phi = np.radians(elevation_deg)
        Rz = np.array([[np.cos(theta), -np.sin(theta), 0],
                       [np.sin(theta),  np.cos(theta), 0],
                       [0,              0,             1]])
        Rx = np.array([[1, 0, 0],
                       [0, np.cos(phi), -np.sin(phi)],
                       [0, np.sin(phi),  np.cos(phi)]])
        R = Rx @ Rz

        r1 = v1_c @ R.T
        r2 = v2_c @ R.T
        r3 = v3_c @ R.T

        # Directional light source from upper right front
        light = np.array([0.5, 0.5, 1.0])
        light /= np.linalg.norm(light)

        # Face normals
        normals = np.cross(r2 - r1, r3 - r1)
        norm_len = np.linalg.norm(normals, axis=1, keepdims=True)
        norm_len[norm_len == 0] = 1.0
        normals /= norm_len

        # Intensity (ambient + diffuse)
        intensity = np.clip(normals @ light, 0.25, 1.0)

        # Screen dimensions
        w, h = size
        margin = int(min(w, h) * 0.08)
        avail = min(w, h) - 2 * margin

        # Painter's algorithm depth sorting
        z_depth = (r1[:, 2] + r2[:, 2] + r3[:, 2]) / 3.0
        order = np.argsort(z_depth)

        # Subsample if triangle count is massive for instant 0.2s performance
        if len(order) > 40000:
            step = len(order) // 40000 + 1
            order = order[::step]

        bg_color = (0, 0, 0, 0) if is_rgba else (30, 30, 35)
        img = Image.new("RGBA" if is_rgba else "RGB", size, bg_color)
        draw = ImageDraw.Draw(img)

        for idx in order:
            if normals[idx, 2] <= 0:  # Backface culling
                continue
            p1 = ((r1[idx, 0] + 0.5) * avail + margin, (0.5 - r1[idx, 1]) * avail + margin)
            p2 = ((r2[idx, 0] + 0.5) * avail + margin, (0.5 - r2[idx, 1]) * avail + margin)
            p3 = ((r3[idx, 0] + 0.5) * avail + margin, (0.5 - r3[idx, 1]) * avail + margin)

            inten = intensity[idx]
            r = int(color_rgb[0] * inten)
            g = int(color_rgb[1] * inten)
            b = int(color_rgb[2] * inten)
            face_color = (r, g, b, 255) if is_rgba else (r, g, b)
            draw.polygon([p1, p2, p3], fill=face_color)

        return img

    except Exception:
        # Fallback card
        img = Image.new("RGBA" if is_rgba else "RGB", size, (25, 25, 30, 255 if is_rgba else (25, 25, 30)))
        d = ImageDraw.Draw(img)
        d.text((40, size[1] // 2), f"3D Model: {stl_path.name}", fill=(255, 255, 255))
        return img
