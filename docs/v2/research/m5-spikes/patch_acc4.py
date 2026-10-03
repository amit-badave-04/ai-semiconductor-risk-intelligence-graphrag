"""Copy model_q8.onnx with accuracy_level=4 set on every MatMulNBits node (activations quantized to int8 per call, so
the kernel no longer dequantizes all weights to fp32 on every call). Reads the repo model, writes ONLY to the scratch dir.
argv[1] = source model, argv[2] = output dir, argv[3] = accuracy level (default 4)."""
import shutil
import sys
from pathlib import Path

import onnx
from onnx import helper

src, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
level = int(sys.argv[3]) if len(sys.argv) > 3 else 4
out_dir.mkdir(parents=True, exist_ok=True)
model = onnx.load(str(src))
n = 0
sample = None
for node in model.graph.node:
    if node.op_type != "MatMulNBits":
        continue
    attrs = {a.name: helper.get_attribute_value(a) for a in node.attribute}
    if sample is None:
        sample = attrs
    kept = [a for a in node.attribute if a.name != "accuracy_level"]
    del node.attribute[:]
    node.attribute.extend(kept)
    node.attribute.append(helper.make_attribute("accuracy_level", level))
    n += 1
print("MatMulNBits nodes patched:", n, "| sample attrs before:", {k: v for k, v in sample.items()})
onnx.save_model(model, str(out_dir / "model_q8.onnx"))
shutil.copy(src.parent / "tokenizer.json", out_dir / "tokenizer.json")
print("saved", (out_dir / "model_q8.onnx").stat().st_size // 1_000_000, "MB")
