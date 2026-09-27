"""Helpers shared by the Blender-side scripts (imported with the script directory on sys.path)."""
import math
import os

import bpy
import numpy as np
from mathutils import Vector


def dims(o):
    """World-space bounds from the vertices themselves: Object.bound_box lags behind transform_apply."""
    bpy.context.view_layer.update()
    n = len(o.data.vertices)
    co = np.empty(n * 3, np.float32)
    o.data.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    m = np.array(o.matrix_world)
    world = co @ m[:3, :3].T + m[:3, 3]
    return Vector(world.min(axis=0).tolist()), Vector(world.max(axis=0).tolist())


def tri_count(o):
    return sum(len(p.vertices) - 2 for p in o.data.polygons)


def select_only(objs):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]


def apply_yaw(o, degrees):
    if not degrees:
        return
    o.rotation_mode = "XYZ"
    o.rotation_euler = (0, 0, math.radians(degrees))
    select_only([o])
    bpy.ops.object.transform_apply(rotation=True)


def studio_hdri():
    """Blender's bundled neutral studio light (softboxes), used for reflections only."""
    root = os.path.dirname(bpy.app.binary_path)
    for dp, _dn, fn in os.walk(root):
        if "studiolights" in dp and dp.replace("\\", "/").endswith("/world"):
            for f in fn:
                if f == "studio.exr":
                    return os.path.join(dp, f)
    return None


def setup_render(size, samples, look="probe"):
    """look="probe": bright, even, grey world - what the segmenter and the facing check want.
       look="preview": neutral studio HDRI for reflections, darker flat backdrop for the camera -
       dark metal stays dark and reads as metal instead of grey plastic (owner, 2026-09-17)."""
    scn = bpy.context.scene
    scn.render.engine = "CYCLES"
    scn.cycles.device = "CPU"
    scn.cycles.samples = samples
    scn.cycles.use_denoising = True
    scn.render.resolution_x = scn.render.resolution_y = size
    scn.render.resolution_percentage = 100
    scn.render.image_settings.file_format = "PNG"
    scn.render.image_settings.color_mode = "RGB"
    try:
        scn.view_settings.view_transform = "Khronos PBR Neutral"
    except TypeError:
        scn.view_settings.view_transform = "Standard"
    w = scn.world or bpy.data.worlds.new("World")
    scn.world = w
    w.use_nodes = True
    nt = w.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    outn = nt.nodes.new("ShaderNodeOutputWorld")
    hdri = studio_hdri() if look == "preview" else None
    if hdri:
        backdrop = (0.38, 0.39, 0.42, 1)
        mix = nt.nodes.new("ShaderNodeMixShader")
        lp = nt.nodes.new("ShaderNodeLightPath")
        env_bg = nt.nodes.new("ShaderNodeBackground")
        flat_bg = nt.nodes.new("ShaderNodeBackground")
        env = nt.nodes.new("ShaderNodeTexEnvironment")
        env.image = bpy.data.images.load(hdri, check_existing=True)
        env_bg.inputs[1].default_value = 1.2
        flat_bg.inputs[0].default_value = backdrop
        nt.links.new(env.outputs[0], env_bg.inputs[0])
        nt.links.new(lp.outputs["Is Camera Ray"], mix.inputs[0])
        nt.links.new(env_bg.outputs[0], mix.inputs[1])
        nt.links.new(flat_bg.outputs[0], mix.inputs[2])
        nt.links.new(mix.outputs[0], outn.inputs[0])
    else:
        bg = nt.nodes.new("ShaderNodeBackground")
        bg.inputs[0].default_value = (0.55, 0.58, 0.63, 1) if look == "probe" else (0.38, 0.39, 0.42, 1)
        bg.inputs[1].default_value = 1.0
        nt.links.new(bg.outputs[0], outn.inputs[0])
    scn["ms_look"] = look
    return scn


VIEW_DIRS = {"posx": (1, 0, 0.12), "negx": (-1, 0, 0.12), "posy": (0, 1, 0.12), "negy": (0, -1, 0.12),
             "iso": (1, -1, 0.7), "side": (0, -1, 0.05), "front": (1, 0, 0.05), "top": (0.001, 0.001, 1)}


class Stage:
    """Temporary lights + camera around `target`; render named views; report camera parameters so a
    later pass can project image-space masks back onto faces."""

    def __init__(self, target, extra_hidden=(), look=None, focus_bounds=None):
        self.target = target
        look = look or bpy.context.scene.get("ms_look", "probe")
        lo, hi = focus_bounds if focus_bounds is not None else dims(target)
        self.centre = (lo + hi) * 0.5
        self.radius = max((hi - lo).length * 0.5, 1e-4)
        self.temps = []
        c, r = self.centre, self.radius

        def light(name, d, energy, size_f, colour=(1, 1, 1)):
            L = bpy.data.lights.new(name, "AREA")
            L.energy = energy * r * r
            L.size = size_f * r
            L.color = colour
            o = bpy.data.objects.new(name, L)
            bpy.context.collection.objects.link(o)
            o.location = c + Vector(d).normalized() * r * 3.5
            o.rotation_euler = (c - o.location).to_track_quat("-Z", "Y").to_euler()
            self.temps.append(o)
        if look == "preview":
            # the studio HDRI does the lighting; a small key shapes the shadows, a rim separates the silhouette
            light("Key", (0.7, -0.8, 0.8), 50, 1.6, (1.0, 0.98, 0.96))
            light("Rim", (-0.6, -0.5, 0.9), 40, 3.0)
        else:
            light("Key", (0.7, -0.8, 0.8), 200, 4.0, (1.0, 0.98, 0.96))
            light("Fill", (-0.7, 0.6, 0.25), 70, 5.0, (0.96, 0.97, 1.0))
            light("Rim", (-0.6, -0.5, 0.9), 110, 3.0)
        cam_d = bpy.data.cameras.new("Cam")
        cam_d.lens = 45
        cam_d.sensor_fit = "AUTO"
        cam_d.clip_start = max(r * 0.001, 1e-5)
        cam_d.clip_end = r * 20
        self.cam = bpy.data.objects.new("Cam", cam_d)
        bpy.context.collection.objects.link(self.cam)
        self.temps.append(self.cam)
        bpy.context.scene.camera = self.cam
        self.hidden = [o for o in bpy.data.objects if o.type == "MESH" and o is not target] + list(extra_hidden)
        for o in self.hidden:
            o.hide_render = True

    def aim(self, view):
        d = Vector(VIEW_DIRS[view]).normalized()
        self.cam.location = self.centre + d * self.radius * 2.6
        up = Vector((0, 0, 1)) if abs(d.z) < 0.95 else Vector((1, 0, 0))
        self.cam.rotation_euler = (self.centre - self.cam.location).to_track_quat("-Z", "Y").to_euler()
        bpy.context.view_layer.update()

    def camera_record(self):
        cd = self.cam.data
        return {"matrix_world": [list(row) for row in self.cam.matrix_world], "lens": cd.lens,
                "sensor_width": cd.sensor_width, "sensor_height": cd.sensor_height, "sensor_fit": cd.sensor_fit,
                "clip_start": cd.clip_start, "clip_end": cd.clip_end}

    def render(self, view, path):
        self.aim(view)
        scn = bpy.context.scene
        scn.render.filepath = path
        bpy.ops.render.render(write_still=True)
        return {"view": view, "file": os.path.basename(path), "camera": self.camera_record(),
                "size": [scn.render.resolution_x, scn.render.resolution_y]}

    def close(self):
        for o in self.hidden:
            o.hide_render = False
        for o in self.temps:
            bpy.data.objects.remove(o, do_unlink=True)


ORTHO_CAMS = {"front": ((1, 0, 0), (0, 0, 1)), "back": ((-1, 0, 0), (0, 0, 1)), "left": ((0, -1, 0), (0, 0, 1)),
              "right": ((0, 1, 0), (0, 0, 1)), "top": ((0, 0, 1), (1, 0, 0)), "bottom": ((0, 0, -1), (1, 0, 0))}


def ortho_camera(cam, view, lo, hi, margin=1.04):
    """Point an orthographic camera at the box (lo, hi) from `view` with a known frame. Returns the record
    {R, U, S, C}: a point p maps to image u = dot(p - C, R) / S + 0.5, v = dot(p - C, U) / S + 0.5."""
    from mathutils import Matrix
    d, up = ORTHO_CAMS[view]
    d, up = Vector(d), Vector(up)
    C = (lo + hi) * 0.5
    ext = hi - lo
    diag = max(ext.length, 1e-4)
    cam.data.type = "ORTHO"
    cam.location = C + d * (diag * 2.0)
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    bpy.context.view_layer.update()
    m = cam.matrix_world.to_3x3()
    U = (m @ Vector((0, 1, 0))).normalized()
    up_p = (up - d * up.dot(d)).normalized()
    ang = math.atan2(d.dot(U.cross(up_p)), U.dot(up_p))
    cam.matrix_world = Matrix.Translation(cam.location) @ (Matrix.Rotation(ang, 4, d) @ cam.matrix_world.to_3x3().to_4x4())
    bpy.context.view_layer.update()
    m = cam.matrix_world.to_3x3()
    R, U = (m @ Vector((1, 0, 0))).normalized(), (m @ Vector((0, 1, 0))).normalized()
    er = abs(ext.x * R.x) + abs(ext.y * R.y) + abs(ext.z * R.z)
    eu = abs(ext.x * U.x) + abs(ext.y * U.y) + abs(ext.z * U.z)
    S = max(er, eu, 1e-4) * margin
    cam.data.ortho_scale = S
    cam.data.clip_start = 0.01
    cam.data.clip_end = diag * 10
    return {"R": [R.x, R.y, R.z], "U": [U.x, U.y, U.z], "S": S, "C": [C.x, C.y, C.z], "extent_r": er, "extent_u": eu, "view": view}


def camera_from_record(rec, name="ProbeCam"):
    cd = bpy.data.cameras.new(name)
    cd.lens = rec["lens"]
    cd.sensor_width = rec["sensor_width"]
    cd.sensor_height = rec["sensor_height"]
    cd.sensor_fit = rec["sensor_fit"]
    cd.clip_start = rec["clip_start"]
    cd.clip_end = rec["clip_end"]
    cam = bpy.data.objects.new(name, cd)
    bpy.context.collection.objects.link(cam)
    from mathutils import Matrix
    cam.matrix_world = Matrix(rec["matrix_world"])
    return cam
