# Eval results — ASRSub v2 (track: eval)

Date: 2026-08-07 · Host: PC user@10.10.20.208 (RTX 3080 10 GB, CUDA 13.3)
Branch: `eval` · Full scripts + commands: `benchmarks/README.md` (repo root of this worktree).
Raw JSON: `benchmarks/results/`.

## 1. ASR fidelity — faster-whisper large-v3-turbo int8 + Silero VAD (ja)

Params: `vad_filter=True`, `min_silence_duration_ms=500`, `language="ja"`,
`condition_on_previous_text=False`, `beam_size=5`, `initial_prompt="こんにちは。これはアニメの台詞です。"`
Same call as production `one_shot_test.py`. 200 utterances per dataset
(seed 42, ≤45 s/utt, ≤2 h), normalized NFKC + strip punctuation.
Metrics: CER (primary) + char-tokenized WER via jiwer.

| dataset | #utt | audio | CER % | WER-char % | naive word WER %* | published large-v3 CER % |
|---|---|---|---|---|---|---|
| reazonspeech_test | 200 | 19.0 min | **13.42** | 13.42 | 61.0 | 14.9 |
| ja_asr.jsut_basic5000 | 200 | 15.6 min | **7.41** | 7.41 | 57.5 | 7.1 |
| ja_asr.common_voice_8_0 | 200 | 16.4 min | **15.20** | 15.20 | 100.0 | 8.5 |

\* English-style word WER is a tokenizer artifact for Japanese (SOTA models
score 55-60%); informational only, never reported as headline.

- ReazonSpeech / JSUT within ~1 pt of the published fp32 large-v3 ceilings;
  turbo-int8 does not regress vs ceiling. No data-leak anomaly (sanity rule
  applies to beating published numbers, which we do not).
- CommonVoice: 15.2% vs 9-11% expectation. Diagnostics: no-VAD CER = 15.15
  (VAD not the cause); seed-7 re-sample = 17.37 (not luck). The mirror test
  pool (4483 utts) is unfiltered crowd speech; errors are mostly homophone /
  kanji substitutions (上水路→増水炉, 社会→会社). Published 8.5% is from a
  filtered kotoba-whisper split — not directly comparable. Documented, not
  tuned around.

## 2. MT — FLORES-200 devtest jpn_Jpan → ind_Latn (300-line sample, seed 42)

| system | spBLEU | COMET (wmt22-comet-da) |
|---|---|---|
| HY-MT1.5-7B-Q4_K_M.gguf (local :8011, single-line requests, temp 0.1) | **19.89** | **0.8848** |
| NLLB-200-3.3B CT2-int8 (local baseline, CPU, beam 4) | **25.44** | **0.8783** |

- HY-MT output is 16% longer than references (ratio 1.16): the model is
  verbose/expands, which penalizes BLEU; merging of short lines is avoided by
  single-line requests (documented caveat). NLLB stays close to ref length
  (ratio 1.02) — BLEU gap (19.89 vs 25.44) is largely length penalty, while
  COMET (which is length-agnostic) ranks them almost equal (0.8848 vs 0.8783).
- Local COMET 0.8848 is NOT comparable to the HY-MT report's XCOMET-XXL 0.8098
  (different metric model + 33x32-pair aggregate; treat that as upper bound only).

## 3. LLM-judge (GEMBA-style pointwise, deepseek-v4-flash via zen/go)

Judge protocol: 1-5 per axis, strict JSON; source = Japanese, target =
Indonesian. 10 requests of 5 lines each.

### a) MT output (FLORES sample, 50 lines)

| axis | mean |
|---|---|
| naturalness | **4.08** |
| fidelity | **3.88** |

Worst 3 by fidelity:
- line 35 (2/5): Chinese characters leaked into Indonesian text
  (`马拉·巴拉苏布拉马尼安`); also "drugged"→"poisoned".
- line 148 (2/5): 慈悲 mistranslated as "Kasih Karunia" (grace).
- line 51 (2/5): voting-envelope conditions conflated.

### b) Real pipeline output (D×D S03E07, 30 random cues, index-parallel)

| axis | mean |
|---|---|
| naturalness | **4.50** |
| fidelity | **3.63** |

Worst 3 by fidelity:
- cue 12 (1/5): SDH placeholder `（Lirik lagu)` kept verbatim — pipeline
  behavior by design (SDH placeholder preservation).
- cue 216 (1/5): subject/object reversal — "アーシア先輩は誰にも渡しません"
  → "Senior Arshia tidak akan memberikannya kepada siapa pun."
- cue 287 (1/5): meaning lost — とっとといかぬか → "Sungguh menyebalkan…".

## 4. Judge backend notes

- `https://api.opencode.ai/zen/v1/chat/completions` (task-specified) returns
  HTTP 403/"Not Found" for this TRANSLATE_API_KEY; verified working route is
  `https://opencode.ai/zen/go/v1/chat/completions` (same key, model
  `deepseek-v4-flash`; the route used by refine_subs.py). Local llama-server
  fallback implemented but not needed.
