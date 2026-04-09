# Real Case Samples

This folder contains two executable-first workload examples:

- `AI News Digest Service` (`single_service`)
- `AI Weekly Research Workflow` (`workflow`)

Files:

- `ai_news_digest.blueprint.json`
- `ai_weekly_research.blueprint.json`
- `openai.runtime_profile.json`
- `ai_news_digest.executable_manifest.json`
- `ai_weekly_research.executable_manifest.json`
- `examples/`

These cases use three step types:

- `python_function`
- `llm_task`
- `artifact_writer`

They are designed to run with an OpenAI model config such as:

- `provider=openai`
- `model_name=gpt-4o-mini`
- `endpoint=https://api.openai.com/v1`
