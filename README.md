# jhe-stress

Scripts for stress-testing JupyterHealth Exchange.

Work in progress.

```bash
export JHE_URL=https://your-jhe.example.local
export JHE_TOKEN=abc123
# fetch records for patient id 40024 400 times,
# with 80 concurrent requests
python3 jhestress.py 40024 --count 400 --concurrency 80 > 80r2w4.csv
```
