#!/usr/bin/env python3
"""Convert GLB/glTF/OBJ assets to web-studio's mesh + UV color texture contract.

Run with Blender, not the system Python:

    blender --background --factory-startup \
      --python tools/blender_standardize_glb.py -- \
      input.glb output.glb --resolution 2048

Output primitives contain triangles, POSITION, NORMAL, TEXCOORD_0, indices and
an embedded PNG baseColorTexture. Material base color textures/factors and
COLOR_0 vertex colors are evaluated by Blender and baked into that texture.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import bpy


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--resolution", type=int, default=2048)
    parser.add_argument("--margin", type=int, default=16)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument(
        "--uv",
        choices=("preserve", "smart"),
        default="preserve",
        help="Preserve a valid UV0, otherwise Smart UV Project; 'smart' always unwraps.",
    )
    parser.add_argument(
        "--apply-modifiers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Apply modifiers (including existing Subdivision/Displace modifiers).",
    )
    parser.add_argument(
        "--bake-mode",
        choices=("color", "combined"),
        default="color",
        help="'color' bakes material/vertex base color without lighting; 'combined' bakes a lit appearance.",
    )
    args = parser.parse_args(argv)
    if not 16 <= args.resolution <= 16384:
        parser.error("--resolution must be between 16 and 16384")
    return args


def reset_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for datablocks in (bpy.data.meshes, bpy.data.materials, bpy.data.images):
        for datablock in list(datablocks):
            if datablock.users == 0:
                datablocks.remove(datablock)


def import_asset(path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(path))
    elif suffix == ".obj":
        # Blender's importer resolves mtllib and texture paths relative to the OBJ.
        bpy.ops.wm.obj_import(filepath=str(path), forward_axis="NEGATIVE_Z", up_axis="Y")
    else:
        raise ValueError(f"Unsupported input format: {suffix}; expected .glb, .gltf, or .obj")


def mesh_objects() -> list[bpy.types.Object]:
    return [obj for obj in bpy.context.scene.objects if obj.type == "MESH" and len(obj.data.polygons)]


def activate_only(obj: bpy.types.Object) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def make_single_user_and_apply_modifiers(obj: bpy.types.Object, enabled: bool) -> None:
    activate_only(obj)
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    if enabled:
        for modifier in list(obj.modifiers):
            try:
                bpy.ops.object.modifier_apply(modifier=modifier.name)
            except RuntimeError as exc:
                print(f"[standardize] warning: cannot apply {obj.name}/{modifier.name}: {exc}")
    triangulate = obj.modifiers.new(name="WebStudio Triangulate", type="TRIANGULATE")
    triangulate.keep_custom_normals = True
    bpy.ops.object.modifier_apply(modifier=triangulate.name)


def ensure_uv0(obj: bpy.types.Object, mode: str) -> None:
    mesh = obj.data
    valid = bool(mesh.uv_layers) and len(mesh.uv_layers[0].data) == len(mesh.loops)
    if valid and mode == "preserve":
        mesh.uv_layers.active_index = 0
        mesh.uv_layers[0].active_render = True
        return

    activate_only(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    if not mesh.uv_layers:
        mesh.uv_layers.new(name="UVMap")
    mesh.uv_layers.active_index = 0
    # A deterministic, non-overlapping fallback suitable for texture baking.
    bpy.ops.uv.smart_project(angle_limit=math.radians(66.0), island_margin=0.02)
    bpy.ops.object.mode_set(mode="OBJECT")
    mesh.uv_layers[0].active_render = True


def ensure_materials(obj: bpy.types.Object) -> list[bpy.types.Material]:
    if not obj.data.materials:
        obj.data.materials.append(make_vertex_color_material(f"{obj.name}_source", obj))
    materials: list[bpy.types.Material] = []
    for index, material in enumerate(obj.data.materials):
        if material is None:
            material = make_vertex_color_material(f"{obj.name}_source_{index}", obj)
            obj.data.materials[index] = material
        if material.users > 1:
            material = material.copy()
            obj.data.materials[index] = material
        material.use_nodes = True
        materials.append(material)
    return materials


def make_vertex_color_material(name: str, obj: bpy.types.Object) -> bpy.types.Material:
    material = bpy.data.materials.new(name=name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    principled = next((node for node in nodes if node.type == "BSDF_PRINCIPLED"), None)
    if principled and getattr(material, "diffuse_color", None):
        principled.inputs["Base Color"].default_value = material.diffuse_color
    if principled and obj.data.color_attributes:
        color = nodes.new("ShaderNodeVertexColor")
        color.layer_name = obj.data.color_attributes[0].name
        material.node_tree.links.new(color.outputs["Color"], principled.inputs["Base Color"])
        material.node_tree.links.new(color.outputs["Alpha"], principled.inputs["Alpha"])
    return material


def add_bake_targets(materials: list[bpy.types.Material], image: bpy.types.Image) -> None:
    for material in materials:
        nodes = material.node_tree.nodes
        node = nodes.new("ShaderNodeTexImage")
        node.name = "__WEB_STUDIO_BAKE_TARGET__"
        node.label = "Web Studio Bake Target"
        node.image = image
        nodes.active = node
        node.select = True


def remove_bake_targets(materials: list[bpy.types.Material]) -> None:
    for material in materials:
        node = material.node_tree.nodes.get("__WEB_STUDIO_BAKE_TARGET__")
        if node:
            material.node_tree.nodes.remove(node)


def bake_object(obj: bpy.types.Object, image: bpy.types.Image, args: argparse.Namespace) -> None:
    materials = ensure_materials(obj)
    add_bake_targets(materials, image)
    activate_only(obj)
    scene = bpy.context.scene
    # Blender's production texture-bake implementation is provided by Cycles.
    scene.render.engine = "CYCLES"
    scene.cycles.samples = args.samples
    scene.cycles.use_denoising = False
    scene.render.bake.margin = args.margin
    scene.render.bake.use_clear = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.exposure = 0
    scene.view_settings.gamma = 1

    if args.bake_mode == "color":
        scene.render.bake.use_pass_direct = False
        scene.render.bake.use_pass_indirect = False
        scene.render.bake.use_pass_color = True
        bake_type = "DIFFUSE"
    else:
        setup_neutral_world_and_lights(obj)
        bake_type = "COMBINED"

    try:
        bpy.ops.object.bake(type=bake_type, save_mode="INTERNAL", use_clear=True, margin=args.margin)
    finally:
        remove_bake_targets(materials)


def setup_neutral_world_and_lights(obj: bpy.types.Object) -> None:
    world = bpy.context.scene.world or bpy.data.worlds.new("Web Studio Bake World")
    bpy.context.scene.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.8, 0.8, 0.8, 1)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.8


def replace_with_baked_material(obj: bpy.types.Object, image: bpy.types.Image) -> None:
    material = bpy.data.materials.new(name=f"{obj.name}_webstudio_baked")
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    principled = nodes.new("ShaderNodeBsdfPrincipled")
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.interpolation = "Linear"
    texture.extension = "REPEAT"
    links.new(texture.outputs["Color"], principled.inputs["Base Color"])
    links.new(texture.outputs["Alpha"], principled.inputs["Alpha"])
    links.new(principled.outputs["BSDF"], output.inputs["Surface"])
    principled.inputs["Metallic"].default_value = 0.0
    principled.inputs["Roughness"].default_value = 1.0
    material.surface_render_method = "DITHERED"
    obj.data.materials.clear()
    obj.data.materials.append(material)
    for polygon in obj.data.polygons:
        polygon.material_index = 0


def standardize(args: argparse.Namespace) -> None:
    input_path = args.input.resolve()
    output_path = args.output.resolve()
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    reset_scene()
    import_asset(input_path)
    objects = mesh_objects()
    if not objects:
        raise RuntimeError("Imported asset contains no renderable mesh polygons")

    baked_images: list[bpy.types.Image] = []
    for index, obj in enumerate(objects):
        print(f"[standardize] processing {index + 1}/{len(objects)}: {obj.name}")
        make_single_user_and_apply_modifiers(obj, args.apply_modifiers)
        ensure_uv0(obj, args.uv)
        image = bpy.data.images.new(
            name=f"{obj.name}_baseColor",
            width=args.resolution,
            height=args.resolution,
            alpha=True,
            float_buffer=False,
        )
        image.generated_color = (1.0, 1.0, 1.0, 0.0)
        image.colorspace_settings.name = "sRGB"
        bake_object(obj, image, args)
        image.pack()
        replace_with_baked_material(obj, image)
        baked_images.append(image)

    activate_only(objects[0])
    for obj in objects:
        obj.select_set(True)
    bpy.ops.export_scene.gltf(
        filepath=str(output_path),
        export_format="GLB",
        use_selection=True,
        export_materials="EXPORT",
        export_image_format="AUTO",
        export_texcoords=True,
        export_normals=True,
        export_tangents=False,
        export_attributes=False,
        export_cameras=False,
        export_lights=False,
        export_animations=False,
        export_skins=False,
        export_morph=False,
        export_apply=True,
        export_yup=True,
        export_draco_mesh_compression_enable=False,
    )
    print(f"[standardize] wrote {output_path} ({output_path.stat().st_size} bytes)")


def main() -> int:
    try:
        standardize(parse_args())
        return 0
    except Exception as exc:
        print(f"[standardize] ERROR: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
