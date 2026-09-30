"""Камень с лаймовыми трещинами для лендинга — процедурная сцена Blender.

    /Applications/Blender.app/Contents/MacOS/Blender -b -P scripts/render-rock.py -- hero OUT.png
    варианты: hero (портрет, груда под карточкой примера), wide (низкая гряда для концовки)

Груда граненых глыб вокруг светящегося ядра: свет ядра пробивается в щели, на гранях —
тонкие прожилки. Свечение (bloom) и растворение краёв в фон страницы — в scripts/rock-post.py.
"""
import math
import os
import random
import sys

import bmesh
import bpy
from mathutils import Euler, Vector

args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else ["hero", "/tmp/rock.png"]
VARIANT = args[0]
OUT = os.path.abspath(args[1])  # Blender считает относительный путь от .blend, которого нет
SAMPLES = int(args[2]) if len(args) > 2 else 256
LIME = (0.578, 1.0, 0.0, 1.0)  # #c8ff00 в линейном цвете
random.seed(11 if VARIANT == "hero" else 23)

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


def node(nt, kind, **props):
    n = nt.nodes.new(kind)
    for k, v in props.items():
        setattr(n, k, v)
    return n


def rock_material():
    m = bpy.data.materials.new("rock")
    nt = m.node_tree
    nt.nodes.clear()
    out = node(nt, "ShaderNodeOutputMaterial")
    coord = node(nt, "ShaderNodeTexCoord")
    base = node(nt, "ShaderNodeBsdfPrincipled")
    base.inputs["Base Color"].default_value = (0.010, 0.010, 0.010, 1)
    base.inputs["Roughness"].default_value = 0.28
    # мелкая шероховатость граней
    grain = node(nt, "ShaderNodeTexNoise")
    grain.inputs["Scale"].default_value = 28
    grain.inputs["Detail"].default_value = 8
    bump = node(nt, "ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.6
    nt.links.new(coord.outputs["Object"], grain.inputs["Vector"])
    nt.links.new(grain.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], base.inputs["Normal"])
    # прожилки: края ячеек Вороного, оставлены только там, где пропускает маска шума
    # координаты искажены шумом — трещины рваные, а не сотовая сетка
    warp = node(nt, "ShaderNodeTexNoise")
    warp.inputs["Scale"].default_value = 2.2
    warp.inputs["Detail"].default_value = 6
    nt.links.new(coord.outputs["Object"], warp.inputs["Vector"])
    centered = node(nt, "ShaderNodeVectorMath", operation="SUBTRACT")
    centered.inputs[1].default_value = (0.5, 0.5, 0.5)
    nt.links.new(warp.outputs["Color"], centered.inputs[0])
    scaled = node(nt, "ShaderNodeVectorMath", operation="SCALE")
    scaled.inputs["Scale"].default_value = 0.45
    nt.links.new(centered.outputs["Vector"], scaled.inputs[0])
    warped = node(nt, "ShaderNodeVectorMath", operation="ADD")
    nt.links.new(coord.outputs["Object"], warped.inputs[0])
    nt.links.new(scaled.outputs["Vector"], warped.inputs[1])
    vor = node(nt, "ShaderNodeTexVoronoi", feature="DISTANCE_TO_EDGE")
    vor.inputs["Scale"].default_value = 1.8
    nt.links.new(warped.outputs["Vector"], vor.inputs["Vector"])
    edge = node(nt, "ShaderNodeMapRange")
    edge.inputs["From Min"].default_value = 0.0
    edge.inputs["From Max"].default_value = 0.024 if VARIANT == "hero" else 0.014  # мельче осколки — уже трещина
    edge.inputs["To Min"].default_value = 1.0
    edge.inputs["To Max"].default_value = 0.0
    nt.links.new(vor.outputs["Distance"], edge.inputs["Value"])
    mask_noise = node(nt, "ShaderNodeTexNoise")
    mask_noise.inputs["Scale"].default_value = 0.9
    nt.links.new(coord.outputs["Object"], mask_noise.inputs["Vector"])
    mask = node(nt, "ShaderNodeMapRange")
    mask.inputs["From Min"].default_value = 0.5
    mask.inputs["From Max"].default_value = 0.56
    nt.links.new(mask_noise.outputs["Fac"], mask.inputs["Value"])
    veins = node(nt, "ShaderNodeMath", operation="MULTIPLY")
    nt.links.new(edge.outputs["Result"], veins.inputs[0])
    nt.links.new(mask.outputs["Result"], veins.inputs[1])
    glow = node(nt, "ShaderNodeEmission")
    glow.inputs["Color"].default_value = LIME
    glow.inputs["Strength"].default_value = 30
    mix = node(nt, "ShaderNodeMixShader")
    nt.links.new(veins.outputs["Value"], mix.inputs["Fac"])
    nt.links.new(base.outputs["BSDF"], mix.inputs[1])
    nt.links.new(glow.outputs["Emission"], mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    return m


def emission_material(strength):
    m = bpy.data.materials.new("core")
    nt = m.node_tree
    nt.nodes.clear()
    out = node(nt, "ShaderNodeOutputMaterial")
    e = node(nt, "ShaderNodeEmission")
    e.inputs["Color"].default_value = LIME
    e.inputs["Strength"].default_value = strength
    nt.links.new(e.outputs["Emission"], out.inputs["Surface"])
    return m


def floor_material():
    m = bpy.data.materials.new("floor")
    nt = m.node_tree
    p = nt.nodes["Principled BSDF"]
    p.inputs["Base Color"].default_value = (0.002, 0.002, 0.002, 1)
    # мокрые пятна: шероховатость гуляет по шуму — отражение рваное, как на асфальте
    coord = node(nt, "ShaderNodeTexCoord")
    n = node(nt, "ShaderNodeTexNoise")
    n.inputs["Scale"].default_value = 1.2
    n.inputs["Detail"].default_value = 6
    nt.links.new(coord.outputs["Object"], n.inputs["Vector"])
    r = node(nt, "ShaderNodeMapRange")
    r.inputs["To Min"].default_value = 0.03
    r.inputs["To Max"].default_value = 0.18
    nt.links.new(n.outputs["Fac"], r.inputs["Value"])
    nt.links.new(r.outputs["Result"], p.inputs["Roughness"])
    return m


ROCK = rock_material()
ROCKS = bpy.data.collections.new("rocks")  # только они получают свет контровых
scene.collection.children.link(ROCKS)


def shard(loc, size, squash=1.0, points=26, stretch=None):
    """Острый осколок: выпуклая оболочка случайных точек — плоские грани и рёбра, как у угля."""
    sx, sy, sz = stretch or (random.uniform(0.7, 1.3), random.uniform(0.7, 1.3), random.uniform(0.6, 1.2))
    bm = bmesh.new()
    for _ in range(points):
        v = Vector((random.gauss(0, 1), random.gauss(0, 1), random.gauss(0, 1))).normalized()
        v *= random.uniform(0.55, 1.0)
        bm.verts.new((v.x * size * sx, v.y * size * sy, v.z * size * sz * squash))
    hull = bmesh.ops.convex_hull(bm, input=bm.verts)
    loose = {v for v in hull["geom_interior"] + hull["geom_unused"] if isinstance(v, bmesh.types.BMVert)}
    bmesh.ops.delete(bm, geom=list(loose), context="VERTS")
    me = bpy.data.meshes.new("shard")
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("shard", me)
    ROCKS.objects.link(ob)
    ob.location = loc
    ob.rotation_euler = Euler([random.uniform(0, math.tau) for _ in range(3)])
    me.materials.append(ROCK)
    return ob


def pile(center, base, top, height, count, size_range, core_strength, ellipse=(1.0, 0.8)):
    """Груда-скала: усечённый конус из осколков, внутри светящееся ядро — свет идёт в щели."""
    cx, cy, _ = center
    ex, ey = ellipse
    bpy.ops.mesh.primitive_cone_add(vertices=48, radius1=base * 0.72, radius2=top * 0.6,
                                    depth=height * 0.92, location=(cx, cy, height * 0.46))
    core = bpy.context.active_object
    core.scale = (ex, ey, 1)
    core.data.materials.append(emission_material(core_strength))
    for _ in range(count):
        # по поверхности конуса; низ чаще и крупнее — груда стоит, а не висит
        h = height * random.random() ** 1.25
        r = base + (top - base) * (h / height)
        a = random.uniform(0, math.tau)
        d = random.uniform(0.82, 1.0)
        s = random.uniform(*size_range) * (1.25 - 0.5 * h / height)
        shard((cx + math.cos(a) * r * d * ex, cy + math.sin(a) * r * d * ey, h), s)
    # верх — рваная площадка из крупных плит
    for _ in range(int(count * 0.12)):
        a = random.uniform(0, math.tau)
        d = random.uniform(0, top)
        s = random.uniform(*size_range) * 1.1
        shard((cx + math.cos(a) * d * ex, cy + math.sin(a) * d * ey, height + random.uniform(-0.12, 0.05)), s,
              squash=0.7)


def debris(center, inner, outer, n, size_range, floating=0, float_z=(1.4, 3.0)):
    cx, cy, _ = center
    for _ in range(n):
        a = random.uniform(0, math.tau)
        d = random.uniform(inner, outer)
        s = random.uniform(*size_range)
        shard((cx + math.cos(a) * d, cy + math.sin(a) * d * 0.8, s * 0.4), s, points=14)
    for _ in range(floating):
        a = random.uniform(0, math.tau)
        d = random.uniform(inner * 0.8, outer * 0.6)
        s = random.uniform(size_range[0], size_range[1] * 1.4)
        shard((cx + math.cos(a) * d, cy + math.sin(a) * d * 0.5 - 0.3, random.uniform(*float_z)), s, points=10)


if VARIANT == "hero":
    pile((0, 0, 0), base=1.2, top=0.75, height=2.0, count=420, size_range=(0.18, 0.36), core_strength=1.6)
    debris((0, 0, 0), 1.6, 3.6, 40, (0.05, 0.14), floating=10, float_z=(2.3, 3.6))
    cam_loc, target, lens, res = (0.0, -8.6, 2.3), (0, 0, 1.55), 72, (1200, 1400)
else:
    pile((-0.5, 0, 0), base=1.9, top=1.2, height=0.95, count=420, size_range=(0.16, 0.32), core_strength=1.6,
         ellipse=(1.0, 0.55))
    pile((2.1, 0.5, 0), base=0.9, top=0.45, height=0.6, count=150, size_range=(0.13, 0.26), core_strength=1.6,
         ellipse=(1.0, 0.7))
    debris((0.4, 0, 0), 2.4, 5.5, 60, (0.03, 0.1), floating=7, float_z=(1.2, 2.2))
    cam_loc, target, lens, res = (0.3, -8.5, 1.2), (0.4, 0, 0.6), 45, (1600, 900)

bpy.ops.mesh.primitive_plane_add(size=60, location=(0, 0, 0))
bpy.context.active_object.data.materials.append(floor_material())

# свет: контровой сверху-сзади, слабая заливка спереди, лаймовая подсветка пола от ядра
bpy.ops.object.light_add(type="AREA", location=(0.5, 3.5, 5.5))
key = bpy.context.active_object
key.data.energy = 700 if VARIANT == "hero" else 380
key.data.size = 3
key.data.spread = math.radians(45)
key.rotation_euler = (math.radians(-35), 0, 0)
key.light_linking.receiver_collection = ROCKS  # пол не ловит блик от лампы
bpy.ops.object.light_add(type="AREA", location=(-4, -5, 3))
fill = bpy.context.active_object
fill.data.energy = 130 if VARIANT == "hero" else 40
fill.light_linking.receiver_collection = ROCKS  # только камни: передние грани не тонут в черноте
# контровые слева и справа сзади — блики по граням, как у угля на карусели
for x in (-3.5, 3.5):
    bpy.ops.object.light_add(type="AREA", location=(x, 2.5, 2.2))
    rim = bpy.context.active_object
    rim.data.energy = 260 if VARIANT == "hero" else 120
    rim.data.size = 1.5
    rim.rotation_euler = (Vector(target) - Vector((x, 2.5, 2.2))).to_track_quat("-Z", "Y").to_euler()
    rim.light_linking.receiver_collection = ROCKS
fill.data.size = 6
fill.rotation_euler = (math.radians(60), 0, math.radians(-40))
bpy.ops.object.light_add(type="POINT", location=(target[0], target[1] - 0.2, 0.25))
spill = bpy.context.active_object
spill.data.energy = 250
spill.data.color = LIME[:3]
spill.data.shadow_soft_size = 1.0

bpy.ops.object.camera_add(location=cam_loc)
cam = bpy.context.active_object
cam.data.lens = lens
cam.rotation_euler = (Vector(target) - Vector(cam_loc)).to_track_quat("-Z", "Y").to_euler()
cam.data.dof.use_dof = True
cam.data.dof.focus_distance = (Vector(target) - Vector(cam_loc)).length
cam.data.dof.aperture_fstop = 4.0
scene.camera = cam

world = bpy.data.worlds.new("w")
world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0, 0, 0, 1)
scene.world = world

scene.render.engine = "CYCLES"
scene.cycles.samples = SAMPLES
scene.cycles.use_denoising = True
try:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "METAL"
    prefs.get_devices()
    for dev in prefs.devices:
        dev.use = True
    scene.cycles.device = "GPU"
except Exception:
    pass
scene.render.resolution_x, scene.render.resolution_y = res
scene.render.resolution_percentage = 100
scene.view_settings.view_transform = "AgX"
scene.view_settings.look = "AgX - Medium High Contrast"
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = OUT
bpy.ops.render.render(write_still=True)
