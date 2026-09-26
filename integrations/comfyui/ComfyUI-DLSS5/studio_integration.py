"""Local studio integration: do not keep untracked guide networks on CUDA.

The upstream guide caches bypass ComfyUI's model manager. Serial video stages
must free the previous diffusion model before starting Depth/RAFT, and release
these private caches before a later H3/LTX queue item. No model math is changed.
"""
import gc


def wrap_easy_pipeline(base, implementation):
    if getattr(base, "_studio_memory_wrapper", False) or base.__name__ == "StudioDLSS5EasyPipeline":
        return base
    class StudioDLSS5EasyPipeline(base):
        _studio_memory_wrapper = True
        def run(self, *args, **kwargs):
            from comfy import model_management
            model_management.unload_all_models()
            model_management.soft_empty_cache()
            try:
                return super().run(*args, **kwargs)
            finally:
                implementation._DEPTH_CACHE.clear()
                implementation._RAFT_CACHE.clear()
                gc.collect()
                model_management.soft_empty_cache()

    return StudioDLSS5EasyPipeline
