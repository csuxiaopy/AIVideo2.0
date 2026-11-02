import hashlib,json,sys
from types import SimpleNamespace
import numpy as np
from backend.detectors.tensorrt_runtime import DetectionModel


def setup_models(monkeypatch,tmp_path,fail_engine=False):
    weights=tmp_path/"detector.pt";weights.write_bytes(b"weights")
    engine=weights.with_suffix(".fp16.engine");engine.write_bytes(b"engine")
    meta={"source_sha256":hashlib.sha256(b"weights").hexdigest(),"engine_sha256":hashlib.sha256(b"engine").hexdigest(),"tensorrt":"test","imgsz":640}
    engine.with_suffix(".json").write_text(json.dumps(meta))
    from contextlib import nullcontext
    monkeypatch.setattr(DetectionModel,"_device_context",lambda self:nullcontext())
    loaded=[]
    class Fake:
        def __init__(self,path,**kw):
            loaded.append(str(path));self.engine=str(path).endswith(".engine")
            self.names={0:"person"};self.predictor=SimpleNamespace(model=SimpleNamespace(format="engine" if self.engine else "pt"));self.calls=0
        def predict(self,**kw):
            self.calls+=1
            if self.engine and fail_engine and self.calls>1:raise RuntimeError("device error")
            return ["ok"]
    monkeypatch.setitem(sys.modules,"ultralytics",SimpleNamespace(YOLO=Fake))
    monkeypatch.setitem(sys.modules,"tensorrt",SimpleNamespace(__version__="test"))
    monkeypatch.setenv("TENSORRT_ENABLED","true")
    return weights,engine,loaded


def test_verified_engine_selected(monkeypatch,tmp_path):
    weights,engine,loaded=setup_models(monkeypatch,tmp_path)
    m=DetectionModel(str(weights),"0")
    assert m.backend=="tensorrt" and m.names=={0:"person"}
    assert m.predict(source=np.zeros((2,2,3)))==["ok"]


def test_checksum_mismatch_falls_back(monkeypatch,tmp_path):
    weights,engine,loaded=setup_models(monkeypatch,tmp_path)
    weights.write_bytes(b"changed weights")
    m=DetectionModel(str(weights),"0")
    assert m.backend=="pytorch" and loaded==[str(weights)]


def test_runtime_failure_retries_same_request_once(monkeypatch,tmp_path):
    weights,engine,loaded=setup_models(monkeypatch,tmp_path,True)
    m=DetectionModel(str(weights),"0")
    assert m.predict(source="original frame")==["ok"]
    assert m.backend=="pytorch" and loaded==[str(engine),str(weights)]
    m.predict(source="next frame");assert len(loaded)==2


def test_disabled_keeps_existing_backend(monkeypatch,tmp_path):
    weights,engine,loaded=setup_models(monkeypatch,tmp_path)
    monkeypatch.setenv("TENSORRT_ENABLED","false")
    assert DetectionModel(str(weights),"0").backend=="pytorch"
    assert loaded==[str(weights)]


def test_model_allowlist_keeps_unvalidated_models_on_pytorch(monkeypatch,tmp_path):
    weights,engine,loaded=setup_models(monkeypatch,tmp_path)
    monkeypatch.setenv("TENSORRT_MODEL_ALLOWLIST","another.pt")
    assert DetectionModel(str(weights),"0").backend=="pytorch"
    assert loaded==[str(weights)]


def test_cuda_device_context_selects_device_and_restores(monkeypatch):
    from contextlib import contextmanager
    state={"current":0,"entered":[]}
    @contextmanager
    def device(number):
        old=state["current"];state["current"]=number;state["entered"].append(number)
        try:yield
        finally:state["current"]=old
    monkeypatch.setitem(sys.modules,"torch",SimpleNamespace(cuda=SimpleNamespace(device=device)))
    model=object.__new__(DetectionModel);model.device="1"
    with model._device_context():assert state["current"]==1
    assert state["current"]==0 and state["entered"]==[1]
