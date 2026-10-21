# SPDX-License-Identifier: AGPL-3.0-only
"""mycelium_openai_shim — OpenAI Chat Completions adapter for Mycelium LiveRoute.

Public entry points:
  * translator.to_mycelium_infer_request(body, request_id)
  * translator.to_openai_chat_response(...)
  * translator.to_openai_chat_chunk(...)
  * translator.build_openai_models_response(current_model)
  * translator.SyncSink / translator.StreamingSink
  * server.serve / server.serve_in_thread
"""
from .translator import (  # noqa: F401
    TranslateError,
    Tokenizer, Detokenizer,
    default_tokenizer, default_detokenizer,
    to_mycelium_infer_request,
    to_openai_chat_response,
    to_openai_chat_chunk,
    build_openai_models_response,
    SyncSink, StreamingSink,
)
