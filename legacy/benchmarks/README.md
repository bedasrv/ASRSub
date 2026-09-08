# Eval Harness — ASR & MT benchmarks

Cross-track contract: see `PLAN.md` (repo root). This directory is owned by the
**eval** track. Scripts run on the PC (`user@10.10.20.208`), GPU box with CUDA
13.3 (whisper needs CUDA 12 libs via `LD_LIBRARY_PATH=/usr/local/lib/ollama/cuda_v12`).

## Environments

- `~/benchmark/venvs/eval` — extra deps (`datasets`, `jiwer`, `sacrebleu`,
  `sentencepiece`, `soundfile`, `pyarrow`, `requests`, `ctranslate2`,
  `unbabel-comet`, `transformers`, `torch`). Install: `pip install -r requirements-eval.txt`.
- `~/benchmark/venvs/fw` — faster-whisper 1.2.1 (ASR eval runs here;
  `pip install jiwer` was added for metrics).

## 1. ASR fidelity (Japanese)

Model: faster-whisper **large-v3-turbo, int8, CUDA** + Silero VAD
(`vad_filter=True`, `min_silence_duration_ms=500`), `language="ja"`,
`condition_on_previous_text=False`, `beam_size=5` — identical to
`one_shot_test.py`'s production call. Metrics via `jiwer`: **CER** (primary) and
**WER with character-based Japanese tokenization** (every char = one token).
English-style word WER is reported only for reference (meaningless for
Japanese; ~57-100% even for near-perfect output) and is never used as a headline.

Normalization (both sides): NFKC + strip punctuation/spaces (`。「」（）…・`).

Datasets: `japanese-asr/ja_asr.*` HF mirrors (not gated; parquet holds
FLAC/audio bytes + transcript). Prepare a fixed 200-utterance subset per
dataset (seed 42, per-utt ≤ 45 s, total ≤ 2 h), decode to 16 kHz WAVs
(48 kHz CommonVoice is resampled with scipy).

```bash
# prepare subset (eval venv)
~/benchmark/venvs/eval/bin/python benchmarks/prepare_asr_subset.py \
  --name reazonspeech_test --parquet-dir <dir with test-*.parquet> --n 200
# transcript + score (fw venv)
cd ~/benchmark/pipeline-eval
LD_LIBRARY_PATH=/usr/local/lib/ollama/cuda_v12 ~/benchmark/venvs/fw/bin/python \
  benchmarks/eval_asr.py --manifest benchmarks/data/<name>/manifest.jsonl --name <name>
```

Results (200 utterances each; `results/asr_<name>.json`):

| dataset | published large-v3 CER (mirror card) | CER | WER (char) | naive word WER* |
|---|---|---|---|---|
| reazonspeech_test (TV-news-like) | 14.9 | **13.42** | 13.42 | 61.0 |
| ja_asr.jsut_basic5000 (clean read) | 7.1 | **7.41** | 7.41 | 57.5 |
| ja_asr.common_voice_8_0 (noisy crowd) | 8.5 | **15.20** | 15.20 | 100.0 |

\* informational only; not a valid Japanese metric.

- ReazonSpeech and JSUT match published large-v3 (fp32) numbers within ~1 pt —
  turbo int8 is not worse than its ceiling.
- CommonVoice 8.0: our number is above the 9-11% expectation. Verified it is
  NOT a VAD artifact (no-VAD CER 15.15 on the same sample) and not sampling
  luck (seed-7 re-sample: 17.37). The mirror's 4483-utterance test pool is
  unfiltered (kotoba-whisper's published number uses a filtered dev/test
  split); homophone/kanji errors dominate (e.g. 上水路→増水炉, 社会→会社).
- Total runtime: ~40 s per dataset on the RTX 3080 (RTF ≈ 0.04-0.05).

## 2. MT quality (ja → id)

Source: FLORES-200 devtest `jpn_Jpan` → `ind_Latn` (1012 lines; official
archive `dl.fbaipublicfiles.com/nllb/flores200_dataset.tar.gz`). Deterministic
300-line sample (seed 42). Current procedure: translate with the local Gemma model
(`http://127.0.0.1:8011/v1`, `/home/user/Documents/Tools/llama-cpp-turboquant/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q6_K_P.gguf`, temp 0.1) using the JSON-array
contract (`source_language`, `target_language`, `lines`). The evaluator sends
one source line per request to keep 1:1 line pairing for BLEU. Historical
HY-MT results remain labeled below.

spBLEU = `sacrebleu` with `-tok flores200` (the flores200 SPM URL is dead; the
identical flores101 SPM model is pre-cached in `~/.sacrebleu/models/`).

```bash
~/benchmark/venvs/eval/bin/python benchmarks/prepare_flores.py --n 300
~/benchmark/venvs/eval/bin/python benchmarks/eval_mt.py --max-lines 300   # -> results/mt_gemma_flores200.json
~/benchmark/venvs/eval/bin/python benchmarks/eval_comet.py                # -> results/comet_flores300.json
~/benchmark/venvs/eval/bin/python benchmarks/eval_nllb.py --max-lines 300 --cpu  # NLLB-200-3.3B CT2-int8 baseline
```

| model | spBLEU | COMET (wmt22-comet-da) |
|---|---|---|
| HY-MT1.5-7B Q4_K_M (local, historical) | **19.89** | **0.8848** |
| NLLB-200-3.3B (CT2 int8, local baseline) | **25.44** | **0.8783** |

Notes:
- COMET (unbabel-comet 2.2.7) runs on the GPU; needed a py3.14 shim patch for
  `functools._HashedSeq` (removed in 3.14) inside the eval venv.
- NLLB-200-3.3B does not fit on the GPU beside llama-server (6 GB used) →
  runs on CPU, ~5.5 s/line.
- Local COMET is not comparable to the XCOMET-XXL 0.8098 in the HY-MT report
  (different model, different aggregate).

## 3. LLM-judge (GEMBA-style pointwise)

Judge: `deepseek-v4-flash` via `TRANSLATE_API_KEY` (from
`~/.config/asr-pipeline/pipeline.env`, never printed) at
`https://opencode.ai/zen/go/v1/chat/completions` (the task-specified
`api.opencode.ai/zen/v1` route 403s with this key; zen/go is the verified
working route also used by refine_subs.py). Falls back to local llama-server
if the API is unreachable. Scores 1-5 per axis with a strict JSON array reply.

```bash
# a) FLORES MT output vs Japanese source (50-line sample)
~/benchmark/venvs/eval/bin/python benchmarks/eval_judge.py --mode mt --n 50
# b) real pipeline output: /tmp/DxD_S03E07.test.id.srt vs /tmp/full_cues.json (30 random cues, parallel by index)
~/benchmark/venvs/eval/bin/python benchmarks/eval_judge.py --mode srt --n 30
```

| sample | naturalness (1-5) | fidelity (1-5) | worst 3 (fidelity) |
|---|---|---|---|
| FLORES-200 MT, 50 lines | **4.08** | **3.88** | line 35 (2): Chinese chars leaked into id ("马拉·巴拉苏布拉马尼安"); line 148 (2): 慈悲→"Kasih Karunia"; line 51 (2): condition mistranslated |
| D×D S03E07 real SRT, 30 cues | **4.50** | **3.63** | cue 12 (1): SDH placeholder `（Lirik lagu)` kept verbatim (pipeline behavior); cue 216 (1): subject/object reversal; cue 287 (1): とっとといかぬか→"Sungguh menyebalkan…" |

## Reproduce

```bash
# venvs
python3 -m venv ~/benchmark/venvs/eval && ~/benchmark/venvs/eval/bin/pip install -r requirements-eval.txt
# (ASR) in fw venv: pip install jiwer
# (COMET) in eval venv: pip install unbabel-comet && pip install "setuptools<81"
```
