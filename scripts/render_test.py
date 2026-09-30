import functools, http.server, threading, json
from pathlib import Path
from yaseo_app import free_audit, score, report
h = functools.partial(http.server.SimpleHTTPRequestHandler, directory="tests/site"); h.log_message=lambda *a,**k: None
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h); threading.Thread(target=srv.serve_forever, daemon=True).start()
r = free_audit.run_isolated(f"http://127.0.0.1:{srv.server_address[1]}/", max_pages=10, allow_private=True)
Path("build/test-result.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
out = report.write(r, Path("build/report-test.html"))
print(out, out.stat().st_size, "байт")
