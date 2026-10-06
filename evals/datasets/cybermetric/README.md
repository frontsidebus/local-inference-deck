# CyberMetric (`cybermetric-80`, `-500`, `-2000`, `-10000`)

General cybersecurity knowledge, as four-option MCQs. The questions were generated with RAG from standards, certifications, papers and books, then human-validated.

- **Source (used):** https://github.com/cybermetric/CyberMetric, pinned to commit `294662b03be73a9c7c73918f687882c1ba637c47` (2026-05-27, raw.githubusercontent URLs).
  - The authors' HF mirror, https://huggingface.co/datasets/tihanyin/CyberMetric (revision `32759c2f…`, last modified 2025-01-21), is **older**. Its content differs:
    - 500: 7 questions differ;
    - 2000: 1 differs, e.g. the mirror has a wrong "Digital Encryption Standard" option that GitHub later corrected;
    - 10000: 11 differ.
  - We use the newer GitHub files.
- **License:**
  - The GitHub repo has **no LICENSE file**; the GitHub license API returns 404.
  - The first author's HF mirror of the same dataset declares `license: apache-2.0` in its card. We record that as the license, with this caveat.
  - Either way, nothing is committed.
- **Paper:** "CyberMetric: A Benchmark Dataset based on Retrieval-Augmented Generation for Evaluating LLMs in Cybersecurity Knowledge", IEEE CSR 2024, https://ieeexplore.ieee.org/document/10679494

```bibtex
@INPROCEEDINGS{10679494,
  author={Tihanyi, Norbert and Ferrag, Mohamed Amine and Jain, Ridhi and Bisztray, Tamas and Debbah, Merouane},
  booktitle={2024 IEEE International Conference on Cyber Security and Resilience (CSR)},
  title={CyberMetric: A Benchmark Dataset based on Retrieval-Augmented Generation for Evaluating LLMs in Cybersecurity Knowledge},
  year={2024}, pages={296-302}, doi={10.1109/CSR61664.2024.10679494}}
```

## Items and scorer

| Suite | Items | sha256 of the source file |
|---|---|---|
| `cybermetric-80` | 80 | `ffdda528c5f344871ad0a9ecef5c64ccf9964d68c3047ac0adaac520b2fc7dc6` |
| `cybermetric-500` | 500 | `b0dcf5cb0b51792e3134bec8cecadfe9c5ecc543fb5abfcece99f041066701a5` |
| `cybermetric-2000` | 2000 | `28129b8d5cae82a80ba76bb31eb029e1238d1e348e6d0e85f44526583e27cd03` |
| `cybermetric-10000` | **10180** (the "10000" file actually holds 10180 questions) | `bd4ff2a96a930211ebd2429639bc386833604c1de783806941541be6b6367ec1` |

- type `mcq`, scorer `mcq_letter`.
- The prompt is the question text, and the choices are sorted A–D.
- The official evaluator asks for `<xml>X</xml>` in a system prompt; we leave the system prompt to the harness so all MCQ suites are prompted the same way.
- There is no category field, so `--sample` is a plain seeded random subset.
- The research notes suggest **CyberMetric-500** as the default; the script writes 80 and 500 unless you pass `--size`.

## Caveats
- **Contamination:** the set has been public since early 2024 and is on several HF mirrors, some of them copies with eval transcripts. Treat scores as a regression floor.
- The sets overlap, but they are not strictly nested (checked by question text): all 80 questions are in the 500 set; 1991 of the 2000 are in the 10000 set; but only 130 of the 500 are in the 2000 set, and 486 in the 10000 set. Report one size, not their sum.
- **Label noise:** LLM-generated, with human checks. The 2026-05 upstream edits show errors were still being fixed.
- The answer letters are balanced in the 500 set (A 125, B 125, C 124, D 126).

## Run
```
python3 evals/datasets/cybermetric/fetch.py [--size 80,500,2000,10000] [--sample 50] [--offline]
```
