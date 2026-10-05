# SecQA (`secqa-v1`, `secqa-v2`)

Computer-security MCQs that GPT-4 generated from the textbook *Computer Systems Security: Planning for Success*. v1 is the easier set and v2 the harder one.

- **Source:** https://huggingface.co/datasets/zefang-liu/secqa, pinned to revision `d00a07484283be5602e2bae36dbefdaaf555a9fb` (`data/secqa_v{1,2}_test.csv`).
- **License:** the card says "**License:** [CC BY-NC-SA 4.0 DEED](https://creativecommons.org/licenses/by-nc-sa/4.0/)", and its front matter has `license: cc-by-nc-sa-4.0`.
- **Paper:** https://arxiv.org/abs/2312.15838

```bibtex
@article{liu2023secqa,
  title={SecQA: A Concise Question-Answering Dataset for Evaluating Large Language Models in Computer Security},
  author={Liu, Zefang}, journal={arXiv preprint arXiv:2312.15838}, year={2023}
}
```

## Items and scorer

| Suite | Items | sha256 of the source file |
|---|---|---|
| `secqa-v1` | 110 | `9a333f23d89d0d3d6e883e5ab3be474327d4f57d2684c3d1ada787d510738bfd` |
| `secqa-v2` | 100 | `b04f92a17b278e9765fa262103fa4127905fbae46d9eaf54f5444f986fc9c722` |

- type `mcq`, scorer `mcq_letter`.
- The source's `Explanation` is kept in `meta.explanation` for error analysis. It is not shown to the model.
- The dev split (5 items, meant for few-shot) and the val split are not converted; the harness runs zero-shot.

## Caveats
- **Small:** 100 items gives a 95% CI of about ±9 points at 70% accuracy. Use it alongside CyberMetric, not instead of it.
- **Contamination:** public since December 2023, and bundled in inspect_evals and other harnesses.
- **Label noise:** GPT-4 generated, from one textbook.
- **License:** non-commercial, share-alike. Not committed; `fetch.py` regenerates it.

## Run
```
python3 evals/datasets/secqa/fetch.py [--versions v1,v2] [--sample 50] [--offline]
```
