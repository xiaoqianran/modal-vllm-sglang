# MiniMax H3 Image - vLLM-Omni

Independent candidate service. Uses minimax-omni.t2i and minimax-omni.i2i; the original minimax service remains SGLang. Shares checkpoint and validator. GPU acceptance required before deployment. Text encoder layer offload, 24 GiB host RAM, max_inputs=2, target_inputs=1, max_containers=2 (one GPU per container; scale to zero; one active engine request plus input preparation or durable publication), GPU snapshot enabled with 2048x1152 six-step warmup; capture and restore pending validation.


The formal SDK image path verifies input references before acquiring the engine lock. It keeps the submitting checkpoint before the engine POST and uses the existing bounded result writer after releasing the engine lock. The per-output claim is retained until publication completes; the existing SDK submission ledger remains the cross-container admission authority. The vLLM-Omni Base64 response protocol is unchanged pending native-file transport verification.
