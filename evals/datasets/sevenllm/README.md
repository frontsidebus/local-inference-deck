# SEvenLLM-Bench (`sevenllm-mcq`, `sevenllm-qa`)

Cyber-threat-intelligence reading and analysis over incident-report text:
- MCQs about a given passage;
- open tasks: extraction (malware features, IOCs, attacker info, time elements) and generation (summaries, threat analysis, response plans, protection strategies).

- **Source:** https://huggingface.co/datasets/Multilingual-Multimodal-NLP/SEVENLLM-Dataset, pinned to revision `1de23ce55cadc984d3f3a7b52c4035a68c6cd5b0` (`test.jsonl`, sha256 `08a27277386b1f205ed0814d379bb0db9bdce8f39bde099020a1f8044d404d7b`).
  - Only the 1300-item benchmark split is used. `train.jsonl`, about 91k instruction items, is not.
- **License:** the dataset card says `license: apache-2.0`.
- **Paper:** https://arxiv.org/abs/2405.03446 (code: https://github.com/CSJianYang/SEevenLLM)

```bibtex
@article{ji2024sevenllm,
  title={SEvenLLM: Benchmarking, Eliciting, and Enhancing Abilities of Large Language Models in Cyber Threat Intelligence},
  author={Ji, Hangyuan and Yang, Jian and Chai, Linzheng and Wei, Chaoren and Yang, Liqun and Duan, Yunlong and Wang, Yunli and Sun, Tianzhen and Guo, Hongcheng and Li, Tongliang and others},
  journal={arXiv preprint arXiv:2405.03446}, year={2024}
}
```

## Items and scorer (English, the default)

| Suite | Items | Type | Scorer | Notes |
|---|---|---|---|---|
| `sevenllm-mcq` | 50 | mcq | `mcq_letter` | prompt = report passage + question |
| `sevenllm-qa` | 600 | extract (300) / freeform (300) | `llm_judge` | `answer` is the dataset's reference output; JSON-dumped for the extraction tasks (`meta.reference_format: json`) |

- The source is half Chinese: of 1300 items, 650 are zh (50 MCQ and 600 QA). `--lang zh` or `--lang all` includes them, and `meta.lang` records the language.
- `meta.category` holds the source's 28 subtask names, e.g.:
  - Threat Analysis;
  - Cybersecurity Event Classification;
  - Malware Feature Extraction.
- `--sample` stratifies on it.

## Scorer notes and gaps
- The paper scores the QA tasks with ROUGE-L and GPT-4 judging. We use `llm_judge`, with the reference answer as `answer`.
- The extraction outputs have free-form JSON keys, so `json_fields` (exact keys) would score near zero. `llm_judge` is the closest contract scorer.
- `f1_tokens` would be a cheap secondary metric, if the harness wants one.

## Caveats
- **Contamination:** the benchmark has been public since May 2024, and the train split was released for fine-tuning. A model tuned on SEVENLLM-Instruct will be inflated.
- **Small MCQ set:** 50 English items gives a 95% CI of about ±13 points.
- **Label noise:** the references were LLM-generated (the "thought" field shows this), so judge agreement with the reference is not the same as correctness.
- **Content:** passages are real CTI reporting and include defanged IOCs (`https[:]//…`) and malware names. They contain no payloads or binaries.
  - The card warns: "these data contain sensitive words in the field of network security, so they may trigger the protection mechanisms of some terminals."
- Not committed, even though Apache-2.0 allows it: the data is about 1 MB, and keeping every suite's data out of git keeps things uniform.

## Run
```
python3 evals/datasets/sevenllm/fetch.py [--lang en|zh|all] [--sample 50] [--offline]
```
