"""Standard-library-only proofs that the optional module is not a core dependency."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from buyer.contracts import ROOT
from tests import test_buyer as buyer_tests
from tests.cli_support import cli_command
from tests.fixtures import CATALOG


class VisionIsolationTest(unittest.TestCase):
    def test_real_server_on_base_python_without_vision_packages(self):
        code = '''
import importlib.util
if any(importlib.util.find_spec(name) for name in ("PIL","numpy","onnxruntime")):
 raise SystemExit(77)
from buyer.demo import DemoServer
from buyer.contracts import ROOT,read_requirements
from buyer.model import ModelQuery,ModelTrace
from threading import Thread
from unittest.mock import patch
from urllib.request import urlopen,Request
import json
with patch("socket.getfqdn",return_value="localhost"):
 server=DemoServer(0,ROOT/"data/catalog.json")
t=Thread(target=server.serve_forever);t.start()
try:
 base="http://127.0.0.1:"+str(server.server_port)
 payload=b'--x\\r\\nContent-Disposition: form-data; name="image"; filename="x.png"\\r\\n\\r\\nabc\\r\\n--x--\\r\\n'
 result=json.load(urlopen(Request(base+"/api/visual-search",data=payload,headers={"Content-Type":"multipart/form-data; boundary=x"})))
 assert result["status"]=="visual_unavailable",result
 assert result["code"]=="missing_dependencies",result
 fields=dict(city_id="taganrog",budget_rub=15000,categories=["table"],width_cm=[],adjustable_height=None,color_temperature_k=[])
 with patch("buyer.demo.extract",return_value=(ModelQuery(read_requirements(fields),()),ModelTrace("mock",1,1,1))):
  payload=json.dumps({"messages":[{"role":"user","text":"Стол в Таганроге, бюджет 15000 рублей"}]}).encode()
  result=json.load(urlopen(Request(base+"/api/buy",data=payload,headers={"Content-Type":"application/json"})))
  assert result["response"]["status"]=="ok",result
finally:
 server.shutdown();t.join();server.server_close()
'''
        done = subprocess.run([sys._base_executable, "-S", "-c", code], cwd=ROOT, text=True, capture_output=True, timeout=25)
        if done.returncode == 77:
            self.skipTest("Base Python also has optional packages installed")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_core_imports_with_all_vision_imports_and_dependencies_forbidden(self):
        code = '''
import importlib.abc,sys
class Deny(importlib.abc.MetaPathFinder):
 def find_spec(self,name,*args):
  if name.startswith(("buyer.vision","onnxruntime","numpy","PIL")): raise ImportError("optional forbidden")
sys.meta_path.insert(0,Deny())
import buyer.demo,buyer.cli
assert not any(name.startswith("buyer.vision") for name in sys.modules)
print("core imports without vision")
'''
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, text=True, capture_output=True, timeout=15)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_worker_timeout_terminates_only_optional_process(self):
        from unittest.mock import Mock
        try:
            from buyer.vision.config import Config
            from buyer.vision.service import VisualService
        except ImportError:
            self.skipTest("Visual module removed")
        service = VisualService(Config(timeout=.01))
        process = Mock(); connection = Mock()
        service.process = process; service.connection = connection
        connection.poll.return_value = False
        process.is_alive.return_value = True
        result = service.search(b"photo", [])
        self.assertEqual(result["status"], "visual_unavailable")
        self.assertEqual(result["code"], "timeout")
        process.terminate.assert_called_once()
        self.assertIsNone(service.process)

    def test_worker_deadline_also_covers_a_blocked_pipe_write(self):
        from threading import Event
        from unittest.mock import Mock
        try:
            from buyer.vision.config import Config
            from buyer.vision.service import VisualService
        except ImportError:
            self.skipTest("Visual module removed")
        service = VisualService(Config(timeout=.01))
        process, connection, released = Mock(), Mock(), Event()
        service.process, service.connection = process, connection
        connection.send.side_effect = lambda _: released.wait(2)
        process.is_alive.return_value = True
        process.terminate.side_effect = released.set
        result = service.search(b"photo", [])
        self.assertEqual(result["code"], "timeout")
        connection.poll.assert_not_called()
        process.terminate.assert_called_once()

    def test_official_data_and_schemas_match_preimplementation_hashes(self):
        import hashlib
        baseline = ROOT / "bench/results/vision-official-before.json"
        if not baseline.exists():
            self.skipTest("Preimplementation integrity report not supplied")
        for row in json.loads(baseline.read_text(encoding="utf-8-sig")):
            with self.subTest(path=row["path"]):
                raw = (ROOT / row["path"].replace("\\", "/")).read_bytes()
                # The archived report predates this checkout. Git's permitted
                # CRLF conversion is not a data edit; same-host byte integrity
                # is checked separately in vision-audit-official-before/after.
                normalized = raw.replace(b"\r\n", b"\n")
                self.assertEqual(hashlib.sha256(normalized).hexdigest().upper(), row["sha256"])


class RemovedVisionCliTest(unittest.TestCase):
    setUp = buyer_tests.CliTest.setUp

    def test_actual_run_and_server_without_any_visual_subsystem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # The copied app physically has no buyer/vision, data/visual, models
            # or cache directories. The real workspace remains intact.
            shutil.copytree(ROOT / "buyer", root / "buyer", ignore=shutil.ignore_patterns("vision", "__pycache__"))
            shutil.copytree(ROOT / "llm", root / "llm", ignore=shutil.ignore_patterns("__pycache__", ".env*"))
            (root / "data").mkdir()
            for name in ("categories.json", "cities.json"):
                shutil.copy2(ROOT / "data" / name, root / "data" / name)
            catalog = root / "data/catalog.json"
            catalog.write_text(json.dumps(CATALOG), encoding="utf-8")
            shutil.copy2(ROOT / "run", root / "run")
            config = os.environ | {"LLM_BASE_URL": f"http://127.0.0.1:{self.server.server_port}/v1",
                "LLM_API_KEY": "offline-only", "LLM_MODEL": "mock", "LLM_USER_AGENT": "vision-isolation-test"}
            done = subprocess.run(cli_command(root / "run", "--catalog", catalog), cwd=root, env=config,
                input=json.dumps({"messages": [{"role": "user", "text": "Стол в Таганроге. Бюджет 6000 рублей"}]}),
                text=True, encoding="utf-8", capture_output=True, timeout=15)
            self.assertEqual(done.returncode, 0, done.stderr)
            response = json.loads(done.stdout)
            self.assertEqual(response["status"], "ok")
            self.assertEqual(set(response), {"status", "options"})
            code = '''
from buyer.demo import DemoServer
from pathlib import Path
from threading import Thread
from urllib.request import urlopen,Request
from urllib.error import HTTPError
import json
with DemoServer(0,Path("data/catalog.json")) as server:
 t=Thread(target=server.serve_forever);t.start()
 try:
  base="http://127.0.0.1:"+str(server.server_port)
  assert urlopen(base+"/").status==200
  assert urlopen(base+"/api/catalog").status==200
  payload=json.dumps({"messages":[{"role":"user","text":"Стол в Таганроге. Бюджет 6000 рублей"}]}).encode()
  result=json.load(urlopen(Request(base+"/api/buy",data=payload,headers={"Content-Type":"application/json"})))
  assert result["response"]["status"]=="ok",result
  payload=b'--x\\r\\nContent-Disposition: form-data; name="image"; filename="x.png"\\r\\n\\r\\nabc\\r\\n--x--\\r\\n'
  result=json.load(urlopen(Request(base+"/api/visual-search",data=payload,headers={"Content-Type":"multipart/form-data; boundary=x"})))
  assert result["status"]=="visual_unavailable",result
 finally: server.shutdown();t.join()
'''
            server_check = subprocess.run([sys.executable, "-c", code], cwd=root, env=config,
                text=True, capture_output=True, timeout=20)
            self.assertEqual(server_check.returncode, 0, server_check.stderr)
