"""Pure safety decisions for finishing; kept free of bpy for regression tests."""


def detail_bake_skip_reason(material_names, preserve_seed_maps=False):
    if preserve_seed_maps:
        return "preserve_seed_maps: retain the source normal/AO instead of generating a replacement"
    # Joined vendor parts each have a full 0..1 atlas. The legacy baker has ONE target image per pass,
    # so different materials write on top of each other. Sharing a UV layer name is not a shared atlas.
    if len(set(material_names)) > 1:
        return "multiple material UV domains: the shared-atlas baker cannot safely combine them"
    return None
