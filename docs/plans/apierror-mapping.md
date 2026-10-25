# 0.x APIError in except clauses and retry tuples

## Request (verbatim)

> Review of llmflows-gptoss-4 found that the fix kept APIError in retry lists
> (call_with_retry(exceptions_to_retry=(APIError, ...))). My understanding:
> in openai 0.x, APIError was one sibling among RateLimitError,
> InvalidRequestError, AuthenticationError, etc., raised mainly for 5xx
> responses; in 1.x, APIError is the base class of every API error, including
> BadRequestError (400) and AuthenticationError (401).
>
> 1. Verify against the real sources first: openai 0.28.1 (openai/error.py,
>    and how api_requestor.py maps status codes to errors) and the 1.x
>    release we pin (_exceptions.py). pip download --no-deps into your
>    scratchpad. Quote the relevant lines. If I'm wrong, stop and tell me.
>
> 2. If it holds: add the mapping to the openai-v1 guide and a [likely]
>    finding for 0.x APIError in an except clause or exceptions tuple.
>    Suggestion: the closest 1.x class is InternalServerError (or drop
>    APIError when InternalServerError is already in the tuple); 1.x
>    APIError would also catch 4xx errors. [likely], not [certain]: a fix
>    that keeps it is listed for review, not rejected.
>
> 3. Report only, don't build: 1.x clients retry 429 and 5xx twice by
>    default (max_retries=2). Confirm in the source. Did 0.x retry anything
>    by default? How much more does code with its own retry loop now retry?
>
> Tests, docs/plans/apierror-mapping.md with this request verbatim plus a
> "Built" section, then commit.

## What the sources say (checked 2026-10-25)

Wheels: openai 0.28.1, 1.55.3 and 2.54.0 (both ends of `PIN_SPEC = ">=1.55.3,<3"`);
requests 2.31.0 and urllib3 2.0.5 for 0.x's transport.

**0.x: APIError is a sibling.** `openai/error.py`: every class subclasses `OpenAIError`
directly (`class APIError(OpenAIError)`, `class InvalidRequestError(OpenAIError)`,
`class AuthenticationError(OpenAIError)`, `class RateLimitError(OpenAIError)`,
`class ServiceUnavailableError(OpenAIError)`, also `TryAgain`, `Timeout`, `APIConnectionError`,
`PermissionError`). `api_requestor.py`:

- `_interpret_response_line`: `if rcode == 503: raise error.ServiceUnavailableError(...)`;
  a body that is not JSON: `raise error.APIError(f"HTTP code {rcode} from API ({rbody})", ...)`.
- `handle_error_response`: `429` -> `RateLimitError`; `[400, 404, 415]` ->
  `InvalidRequestError`; `401` -> `AuthenticationError`; `403` -> `PermissionError`; `409` ->
  `TryAgain`; `elif stream_error:` -> `APIError` (error event in a stream); `else:` ->
  `APIError` (every other status: 500, 502, 504, 408, 422, ...); a response without an `error`
  key -> `APIError("Invalid response object from API ...")`.

So 0.x `APIError` meant: 5xx other than 503, statuses without their own class, malformed
responses, and mid-stream errors. Never 400/401/403/404/409/429.

**1.x: APIError is the base.** `_exceptions.py` (same in 1.55.3 and 2.54.0):
`class APIError(OpenAIError)`, `class APIStatusError(APIError)`,
`class APIConnectionError(APIError)`, `class APITimeoutError(APIConnectionError)`,
`class BadRequestError(APIStatusError)` (400), `AuthenticationError` (401),
`PermissionDeniedError` (403), `NotFoundError` (404), `ConflictError` (409),
`UnprocessableEntityError` (422), `RateLimitError` (429), `class InternalServerError(APIStatusError)`.
`_client.py` `_make_status_error`: `if response.status_code >= 500: return
_exceptions.InternalServerError(...)`, other unmapped statuses `return APIStatusError(...)`.
`_streaming.py` raises the base class for an error event in a stream: `raise APIError(message=message,
request=self.response.request, body=data["error"])`.

The understanding holds. Closest 1.x class for a retry: `InternalServerError` (all 5xx; 0.x
split 503 off as ServiceUnavailableError, which also maps to it).

## Report: retries (item 3, not built)

**1.x retries by default.** `_constants.py`: `DEFAULT_MAX_RETRIES = 2`,
`INITIAL_RETRY_DELAY = 0.5`, `MAX_RETRY_DELAY = 8.0`; both clients take
`max_retries: int = DEFAULT_MAX_RETRIES`. `_base_client.py` `_should_retry`: the
`x-should-retry` header when present, else `408`, `409`, `429` and `>= 500`. The request loop
also retries timeouts (`except httpx.TimeoutException` / `timeout_exceptions()`) and connection
errors (`except Exception`) while `remaining_retries > 0`. Delay: `Retry-After` when given (up to
60 s in 1.55.3; 2.54.0 does not retry at all when `Retry-After` exceeds
`MAX_RETRY_AFTER_DELAY = 2 * 60`), else `min(0.5 * 2**n, 8.0)` with up to 25 % jitter: about
0.5 s then 1 s.

**0.x retried only failed connects.** `api_requestor.py`: `MAX_CONNECTION_RETRIES = 2`,
`s.mount("https://", requests.adapters.HTTPAdapter(max_retries=MAX_CONNECTION_RETRIES))`.
requests turns that into `Retry.from_int(2)` (urllib3 `Retry(total=2)`): no
`status_forcelist`, and `is_retry` starts with `if not self._is_method_retryable(method):
return False`, where `DEFAULT_ALLOWED_METHODS` excludes POST, so no status is retried for API
calls (the Retry-After path for 413/429/503 sits behind the same method check). Read errors
are retried only for allowed methods either: `if read is False or method is None or not
self._is_method_retryable(method)`. Connect errors are retried regardless of method. The async
path (`aiohttp.ClientSession()`) has no retries at all.

**Effect on code with its own loop.** A loop of N attempts that catches 429/5xx now makes up to
3N HTTP requests on those errors (N before), and each attempt can take about 1.5 s of client
backoff (up to the Retry-After value, 60-120 s) before the loop's own sleep. Timeouts and read
errors: 1 request per attempt in 0.x, 3 in 1.x, and the default timeout is 600 s, so N
attempts can now wait 3N x 600 s. Failed connects: 3 per attempt in both (sync); async 0.x
made 1. 408 and 409: 0.x raised `APIError` / `TryAgain` once per attempt; 1.x retries them
too. If the loop also keeps 1.x `APIError` in its list, 400/401/403/404/422 are now retried by
the loop (once per attempt; the client does not retry them). Code that wants its old count
passes `max_retries=0` to the client, or drops its own loop.

## Plan

1. `rules.py`: an `APIERROR_NOTE` (0.x sibling vs 1.x base) on the `APIError` row of
   `ERROR_CLASSES`, so the `from openai import APIError` import finding and the CERTAIN
   `openai.error.APIError` finding carry it. A `CATCH_APIERROR` suggestion: closest 1.x class
   `openai.InternalServerError`, or drop APIError when the tuple already has
   InternalServerError / ServiceUnavailableError; 1.x APIError also catches 4xx.
2. `python_checks.py`: a [likely] `error_class` finding on a name for APIError in an `except`
   type or a tuple/list/set literal. The forms 1.x code also uses (`openai.APIError`, or the
   name after `from openai import APIError`) are ambiguous: a lone `except openai.APIError:` is
   the normal 1.x catch-all. They are reported when the same literal names another openai
   error class (in 1.x the others are redundant subclasses, a sign of 0.x semantics) or the
   file has other 0.x findings. A name imported from `openai.error` is always reported.
   `openai.error.APIError` written out is already a CERTAIN finding (with the note), so no
   second finding on it. So the llmflows fix, which kept `APIError` next to
   `InternalServerError`, is listed for review (`remaining_likely_finding`), not rejected.
3. Guide, errors part: the APIError row and a short paragraph on what 0.x APIError covered.
4. notes.md: the limitation (a lone `except openai.APIError:` in a file without other
   findings is not reported; aliases through assignment are not followed).
5. Tests in tests/test_scanner.py.

## Built

- `rules.py`: `APIERROR_NOTE` on the `APIError` row of `ERROR_CLASSES` (the import finding now
  reads "from openai import APIError; 0.x APIError was a sibling of ..."), `CATCH_APIERROR` /
  `CATCH_APIERROR_DROP`, `SERVER_ERRORS`, `ERROR_NAMES` (0.x and 1.x error class names), and
  `ERROR_MODULE_GONE` (the "openai.error is gone" text, now shared).
- `python_checks.py`: `visit_ExceptHandler` and `visit_Tuple` / `List` / `Set` -> `_catch`,
  as planned. Skipped in test files. Forms 1.x also uses with no sibling are held in
  `catch_apierror` and reported after the walk if the file has other findings.
  `openai.error.APIError` written out in a catch is one [certain] finding, "openai.error is
  gone: ...; <catch suggestion>", in place of the plain attribute finding.
- On the llmflows files (`tests/fixtures/llmflows_attempt6/openai_embeddings_*`): the
  original's `exceptions_to_retry=(APIError, ..., ServiceUnavailableError)` gets "drop it,
  ServiceUnavailableError (1.x openai.InternalServerError, every 5xx) is already listed"; the
  fix's `(APIError, ..., InternalServerError)` gets "drop it, InternalServerError (every 5xx)
  is already listed", [likely], so the verifier lists it as `remaining_likely_finding`.
- Guide (errors part): APIError has its own row, then a paragraph on what 0.x APIError
  covered, what 1.x APIError catches, and a retry-list before/after example.
- notes.md: the limitations (scanner).
- Tests (tests/test_scanner.py): retry tuples (plain, with ServiceUnavailableError), lone
  `except` of a name from openai.error, `openai.error.APIError` written out; the fix's form is
  [likely] and not in `incomplete_findings`; not reported: lone `except openai.APIError:` in
  1.x code, a tuple without another openai error, `{OpenAIError, APIError}`, a test file;
  reported: a lone `except openai.APIError:` in a file with 0.x calls.
