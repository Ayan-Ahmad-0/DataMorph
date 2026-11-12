"""Google Gemini API client integration for structured recipe and report generation."""

from __future__ import annotations
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load local environment variables from .env
load_dotenv()

# Circuit breaker state: skip API calls if quota was recently exhausted
_circuit_broken_until: float = 0.0
_circuit_breaker_reason: str = ""


def _call_with_timeout(fn: Callable[[], Any], timeout_s: float) -> Any:
    """Run blocking SDK call on a daemon thread without blocking caller or shutdown on timeout."""
    result_box: Dict[str, Any] = {}
    done = threading.Event()

    def _runner() -> None:
        try:
            result_box["value"] = fn()
        except BaseException as exc:
            result_box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_runner, daemon=True, name="gemini-recipe-call").start()
    if not done.wait(timeout=timeout_s):
        raise TimeoutError(f"Timed out after {timeout_s:.1f}s")
    if "error" in result_box:
        raise result_box["error"]
    return result_box.get("value")


def get_gemini_callable(api_key: Optional[str] = None, model_name: str = "gemini-3.5-flash"):
    """
    Returns a callable `(system_prompt: str, user_prompt: str) -> str`
    powered by the Google GenAI SDK.
    Includes fast failover and a circuit breaker for quota/rate limit errors.
    """
    global _circuit_broken_until, _circuit_breaker_reason

    # If circuit breaker is active, skip remote call immediately (< 0.1ms)
    if time.time() < _circuit_broken_until:
        logger.info(
            "Gemini API circuit breaker active (for next %ds): %s. Using instant engine.",
            int(_circuit_broken_until - time.time()),
            _circuit_breaker_reason,
        )
        return None

    key = api_key or os.getenv("GEMINI_API_KEY")
    if not key or not key.strip():
        return None

    try:
        from google import genai
        client = genai.Client(api_key=key.strip())

        def gemini_call(system_prompt: str, user_prompt: str) -> str:
            global _circuit_broken_until, _circuit_breaker_reason

            candidate_models = list(dict.fromkeys([model_name, "gemini-flash-latest"]))
            call_timeout = float(os.getenv("GEMINI_RECIPE_TIMEOUT_S", "5.0"))
            last_err: Optional[BaseException] = None

            for m in candidate_models:
                try:
                    def do_call(target_model: str = m):
                        return client.models.generate_content(
                            model=target_model,
                            contents=user_prompt,
                            config={
                                "system_instruction": system_prompt,
                                "response_mime_type": "application/json",
                            },
                        )

                    response = _call_with_timeout(do_call, call_timeout)
                    return response.text

                except TimeoutError:
                    logger.warning(
                        "Gemini model %s timed out after %.1fs. Trying next fallback.",
                        m,
                        call_timeout,
                    )
                    last_err = TimeoutError(f"Gemini call to {m} timed out after {call_timeout:.1f}s")
                    continue

                except Exception as e:
                    err_str = str(e).lower()
                    last_err = e

                    # If quota exhausted (429), trip circuit breaker for 3 minutes and break immediately
                    if "429" in err_str or "resource_exhausted" in err_str or "quota" in err_str:
                        _circuit_broken_until = time.time() + 180.0
                        _circuit_breaker_reason = "Free tier quota exhausted (429)"
                        logger.warning("Gemini quota exhausted (429). Tripping circuit breaker for 3m: %s", e)
                        break
                    elif "401" in err_str or "403" in err_str or "unauthenticated" in err_str or "permission" in err_str:
                        _circuit_broken_until = time.time() + 300.0
                        _circuit_breaker_reason = "API authentication or permission error"
                        logger.warning("Gemini auth error. Tripping circuit breaker for 5m: %s", e)
                        break
                    else:
                        logger.warning("Gemini model %s failed: %s", m, e)
                        continue

            raise last_err

        return gemini_call
    except Exception as e:
        logger.warning("Failed to initialize Google GenAI client: %s", e)
        return None
