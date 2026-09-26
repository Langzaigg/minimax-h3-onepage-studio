"""Add Tujiang Studio nodes while retaining upstream exports and mappings."""
try:
    from .studio_filter import make_filter_node
    from .studio_integration import wrap_easy_pipeline
except ImportError:
    from studio_filter import make_filter_node
    from studio_integration import wrap_easy_pipeline


def register_nodes(node_classes, display_names, implementation):
    classes, names = dict(node_classes), dict(display_names)
    classes["DLSS5EasyPipeline"] = wrap_easy_pipeline(classes["DLSS5EasyPipeline"], implementation)
    classes["StudioDLSS5Filter"] = make_filter_node(implementation)
    names.setdefault("StudioDLSS5Filter", "Studio: DLSS NR Controls")
    return classes, names
