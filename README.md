# 映序 / Inkseq · H3 Studio

**New development session:** read the short [AGENTS.md](AGENTS.md) entry and the assigned issue's latest owner comments and claims on the [Inkseq Delivery board](https://github.com/orgs/inkseq/projects/1). Expand linked contracts only as needed; [workflow/project.json](workflow/project.json) contains board identifiers. Preserve older checkouts. Use English on GitHub; merged source, a published release and verified live usability are separate states.

Inkseq provides one business API for Quick Chat, Yingxu story creation and external agents at **https://inkseq.com**. The `www` hostname and the previous product domains redirect to that origin after the verified cutover. Host-bound login cookies require one new login; existing API keys and Agent grants keep their original authority and expiry. Follow the public [Agent guide](https://inkseq.com/for-agents/guide.json) or [download the skill](https://inkseq.com/for-agents/skill.zip).

The generation runtime uses WanGP; historical Comfy routes remain for original-job recovery and rollback. Backend source is `platform_app.py` / `studio_platform/`; deployment definitions are in `deploy/platform/`. Internal `sixnine` identifiers, tenant IDs and encrypted credential records remain compatible. These source boundaries do not establish live GPU availability.

## Documentation map

| Need | Entry and authority |
|---|---|
| Product direction and work packages | [PROJECT-PLAN.md](PROJECT-PLAN.md) |
| Durable choices, rationale and revisit conditions | [DECISIONS.md](DECISIONS.md); acceptance of a decision is not release evidence |
| Required generation behavior | [GENERATION-CONTRACT.md](GENERATION-CONTRACT.md) and the specific contract/specification linked by the issue |
| Latest recorded implementation/deployment observations | [CURRENT-BASELINE.md](CURRENT-BASELINE.md); dated evidence, not live health |
| Ownership, current task status and acceptance | [Delivery board](https://github.com/orgs/inkseq/projects/1), linked issues/PRs and [WORKFLOW.md](WORKFLOW.md) |
| Historical design, implementation reports and local research | [PLANNING-INDEX.zh-CN.md](PLANNING-INDEX.zh-CN.md); follow its scope and date labels |

Material durable decision changes update the record and affected plan/contract together. Historical reports explain a past revision; they do not override current contracts or authorize operating a service.

## Working references

- [Development checks and scoped releases](DEVELOPMENT-RELEASE.zh-CN.md), [platform deployment](deploy/platform/README.zh-CN.md), [release and rollback](deploy/platform/RELEASE.zh-CN.md).
- [API usage](API-USAGE.zh-CN.md), [backup/recovery](BACKUP-RECOVERY.zh-CN.md) and [operations](OPERATIONS.zh-CN.md): use the applicable implementation/version and current authorization; these procedures are not health reports.
- [Earlier architecture](ARCHITECTURE.zh-CN.md), [five-minute walkthrough](START-HERE.zh-CN.md), [delivery report](DELIVERY-REPORT.zh-CN.md), [readiness review](deploy/platform/READINESS-REVIEW.zh-CN.md) and [iteration evidence](ITERATIONS.zh-CN.md): dated background, not the active task list or proof of today's deployment.
- [Infrastructure/cost observations](deploy/platform/INFRASTRUCTURE.zh-CN.md) and [DNS cutover](deploy/platform/DNS-CUTOVER.zh-CN.md): verify the recorded scope, destination and date before use; old prices, endpoints and rollout windows are not current authority.

Canonical frontend and approved mock live in the public `inkseq/sixnine-design` repository at `../video-studio-design/studio-app` and `../video-studio-design/quick-chat-mock`. The backend is `inkseq/h3-studio`. The active Delivery Project is the private `inkseq` organization project; its verified item, field and view identifiers are recorded in `workflow/project.json`. Historical evidence retains its original links. This repository's `yingxu/` is a generated release snapshot maintained by `tools/sync_yingxu_source.py`. Edit canonical source; do not directly edit the snapshot, publish unapproved UX, or put local drafts/media into images.

## Historical standalone workbench

The earlier MiniMax H3 workbench exposed separate image/video/audio references, timeline guides, native controls, owned assets, jobs and downloads. `server.py`, `web/` and the old standalone deployment files remain for known compatibility/laboratory uses; this is not a second business-backend development path.

Its older documentation described a CPU-only website deployment with generation disabled and a fixed test account set. That description is historical, not the current public generation or authentication state. Consult the dated baseline and exact release evidence before operating any deployment.

- [Lightsail and GitHub Actions deployment](LIGHTSAIL.md)
- [Multi-GPU / API routing design and remaining work](SCALING.zh-CN.md)
- [Generation controls and measured limits](CONTROLS.zh-CN.md)

## Local checks

Use [the scoped development/release workflow](DEVELOPMENT-RELEASE.zh-CN.md) to select checks for the final change. The broad compatibility command below is a reference, not a requirement to run the full suite for every small edit.

Python 3.12, Node.js and FFmpeg/ffprobe are required:

```sh
python -m pip install -r requirements.lock.txt
python -m unittest discover -s . -p 'test_*.py' -v
node --check web/app.js
```

Tests use disposable data and fake requests. Do not run live generation scripts
as a CI test. Database, media, cloud state, SSH files, model weights, credentials,
logs and local experiments are deliberately excluded from Git and Docker.

GitHub Actions runs CI on push/PR. CPU deployment is an explicit main-branch
workflow dispatch after destination configuration and account provisioning.
The tested image is passed to deployment unchanged. This repository alone does
not create an AWS account, Lightsail instance, domain, GPU, or API subscription.
