# MiniMax H3 Image - vLLM-Omni

Independent candidate service. Uses minimax-omni.t2i and minimax-omni.i2i; the original minimax service remains SGLang. Shares checkpoint and validator. GPU acceptance required before deployment. Text encoder layer offload, 24 GiB host RAM, max_inputs=4, GPU snapshot enabled with 2048x1152 six-step warmup; capture and restore pending validation.
