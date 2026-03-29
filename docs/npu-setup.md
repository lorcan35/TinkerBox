# NPU Setup — Qualcomm QCS6490 (Dragon Q6A)

## Overview

The Dragon Q6A (Radxa) has a Qualcomm QCS6490 SoC with a Hexagon DSP (HTP v68)
that can run quantized LLMs at ~8 tok/s — a 30x improvement over Ollama on CPU
(~0.24 tok/s for gemma3:4b).

## Hardware Prerequisites

- Radxa Dragon Q6A with QCS6490
- `/dev/fastrpc-cdsp` present (Hexagon DSP access)
- `radxa` user in `render` group
- Packages: `fastrpc`, `libcdsprpc1`, `radxa-firmware-qcs6490`

Check:
```bash
ls -la /dev/fastrpc-cdsp   # Should exist
groups radxa               # Should include 'render'
dpkg -l | grep -E 'fastrpc|cdsprpc|firmware-qcs6490'
```

## QAIRT SDK Installation

### 1. Download SDK (on a fast machine)

Download QAIRT v2.37.1 from Qualcomm:
- File: `qairt-v2.37.1.zip` (~1.3GB)
- Extract with `7z` (not `unzip` — large zip handling issues)

```bash
7z x qairt-v2.37.1.zip -oextract
```

### 2. Extract aarch64 Linux files

From the SDK, you need:

**Binaries** (`bin/aarch64-oe-linux-gcc11.2/`):
- `genie-t2t-run` (104KB) — text-to-text inference runner
- `genie-app` (280KB) — general Genie app
- `qnn-platform-validator` (358KB) — hardware validation tool

**Libraries** (`lib/aarch64-oe-linux-gcc11.2/`):
- `libGenie.so` (7MB) — Genie runtime
- `libQnnHtp.so` (2.4MB) — HTP backend
- `libQnnHtpPrepare.so` (68MB) — HTP graph preparation
- `libQnnHtpV73Stub.so` (278KB) — V73 host stub
- `libQnnSystem.so` (4.2MB) — QNN system
- `libQnnGenAiTransformer.so` + related — GenAI ops
- `libcalculator.so`, `libQnnModelDlc.so`, `libQnnIr.so`

**Hexagon skel libs** (`lib/hexagon-v73/unsigned/`):
- `libQnnHtpV73Skel.so` — DSP-side skel
- `libQnnHtpV73.so` — DSP-side HTP
- `libCalculator_skel.so` — Calculator skel

### 3. Deploy to Dragon

```bash
ssh radxa@192.168.1.89  # password: radxa
mkdir -p /home/radxa/qairt/{bin,lib,lib/hexagon-v73}
```

SCP from build machine:
```bash
scp bins/* radxa@192.168.1.89:/home/radxa/qairt/bin/
scp libs/* radxa@192.168.1.89:/home/radxa/qairt/lib/
scp hexagon-v73/* radxa@192.168.1.89:/home/radxa/qairt/lib/hexagon-v73/
chmod +x /home/radxa/qairt/bin/*
```

### 4. Environment Setup

Add to `~/.bashrc`:
```bash
export QAIRT_HOME=/home/radxa/qairt
export LD_LIBRARY_PATH=/home/radxa/qairt/lib:$LD_LIBRARY_PATH
export ADSP_LIBRARY_PATH=/home/radxa/qairt/lib/hexagon-v73
export PATH=/home/radxa/qairt/bin:$PATH
```

## Model Installation

### Llama 3.2 1B (Quantized for QCS6490)

```bash
pip3 install --break-system-packages modelscope
modelscope download --model radxa/Llama3.2-1B-4096-qairt-v68 \
    --local_dir /home/radxa/qairt/models/llama32-1b
```

This downloads:
- `weight_sharing_model_1_of_1.serialized.bin` (1.66GB) — quantized weights
- `htp-model-config-llama32-1b-gqa.json` — Genie config
- `htp_backend_ext_config.json` — HTP backend config (soc_id=35, dsp_arch=v68)
- `tokenizer.json` — Llama tokenizer
- Bundled libs (genie-t2t-run, libGenie.so, etc.)

### Test

```bash
cd /home/radxa/qairt/models/llama32-1b
export LD_LIBRARY_PATH=$PWD:/home/radxa/qairt/lib:$LD_LIBRARY_PATH
export ADSP_LIBRARY_PATH=$PWD:/home/radxa/qairt/lib/hexagon-v73
chmod +x genie-t2t-run
./genie-t2t-run -c htp-model-config-llama32-1b-gqa.json -p "Hello, who are you?"
```

Expected output:
```
[PROMPT]: Hello, who are you?
[BEGIN]:  I am a friendly AI assistant...[END]
```

## Performance

| Backend | Model | Speed | Notes |
|---------|-------|-------|-------|
| Ollama (CPU) | gemma3:4b | ~0.24 tok/s | ARM64 Cortex-A78 |
| NPU Genie (HTP) | Llama 3.2 1B | ~8 tok/s | QCS6490 Hexagon DSP |

NPU is **~30x faster** than CPU for LLM inference.

## Model Size Limitations (HTP v68)

The QCS6490's Hexagon DSP presents as **HTP v68**. Genie context binaries are
compiled for a specific HTP instruction set and are NOT cross-compatible.

| Model | v68 (QCS6490) | v73+ (8 Gen 2+) |
|-------|---------------|------------------|
| Llama 3.2 1B | Yes (radxa/modelscope) | Yes |
| Llama 3.2 3B | **No** — no binaries exist | Yes (HuggingFace, AI Hub) |
| Llama 3.1 8B+ | No | Varies by SoC |

**The blocker for 3B is HTP architecture, not RAM.** Dragon has 12GB RAM (enough
for 3B weights at ~2.5GB), but no v68-compatible 3B context binaries exist.
Sources checked (2026-03-29): HuggingFace Volko76, Radxa ModelScope, Qualcomm AI Hub.

To run 3B+ on NPU, you need a v73+ board (Snapdragon 8 Gen 2 or newer).

## TinkerBox Integration

Set `llm.backend: npu_genie` in `config.yaml`:

```yaml
llm:
  backend: npu_genie
  genie_model_dir: /home/radxa/qairt/models/llama32-1b
  genie_config: htp-model-config-llama32-1b-gqa.json
```

Or via environment variable:
```bash
export DRAGON_VOICE_LLM_BACKEND=npu_genie
```

## Troubleshooting

### qnn-platform-validator fails with V68 error
The validator hardcodes V68 as first probe — this is expected on V73 hardware.
The actual genie-t2t-run works fine regardless.

### "rpcmem_init" warnings
Normal: `dummy call to rpcmem_init, rpcmem APIs will be used from libxdsprpc`.
This is informational from the fastrpc layer, not an error.

### Model load takes ~2s
First invocation loads the 1.66GB model into shared memory. Subsequent runs
in the same process would be faster, but genie-t2t-run is stateless (new process
per request). Future optimization: persistent Genie server process.
