import vcr as _vcr


def _strip_request(request):
    headers = getattr(request, "headers", {})
    keys = [key for key in list(headers.keys()) if key.lower() == "accept-encoding"]
    for key in keys:
        headers.pop(key, None)
    return request


def _strip_response(response):
    headers = response.get("headers", {})
    keys = [key for key in headers.keys() if key.lower() in {"content-encoding", "transfer-encoding"}]
    for key in keys:
        headers.pop(key, None)
    return response


vcr = _vcr.VCR(
    serializer="yaml",
    record_mode="new_episodes",
    match_on=["uri", "method", "path", "query", "body"],
    record_on_exception=False,
    before_record_request=_strip_request,
    before_record_response=_strip_response,
)
