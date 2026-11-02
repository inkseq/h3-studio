"""One web report, one email to the team, through SES with a send-only key.

The app has no instance role, so the key comes from runtime-secrets like the other
vendor credentials. Its IAM policy allows only this sender and this recipient.
"""

import json
from pathlib import Path

FILENAME = "feedback-mail.json"
FIELDS = {"access_key_id", "secret_access_key", "region", "sender", "recipient"}
CATEGORY_NAMES = {
    "page": "页面或按钮",
    "generation": "生成视频或图片",
    "payment": "付款或积分",
    "account": "账号",
    "other": "其他",
}


def load_config(data_dir):
    path = Path(data_dir) / "runtime-secrets" / FILENAME
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != FIELDS
        or not all(isinstance(item, str) and item for item in value.values())
    ):
        raise ValueError("feedback_mail_config_invalid")
    return value


def _client(config):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "sesv2",
        region_name=config["region"],
        endpoint_url=f"https://email.{config['region']}.amazonaws.com",
        aws_access_key_id=config["access_key_id"],
        aws_secret_access_key=config["secret_access_key"],
        config=Config(
            connect_timeout=5,
            read_timeout=10,
            retries={"mode": "standard", "total_max_attempts": 2},
        ),
    )


def compose(report, profile, environment):
    body = report["body"]
    short = report["report_id"][:8].upper()
    category = CATEGORY_NAMES.get(body["category"], body["category"])
    label = "" if environment == "production" else f"[{environment or 'local'}] "
    lines = [
        body["summary"],
        "",
        "----",
        "编号: " + short,
        "类型: " + category,
        "账号: "
        + " ".join(filter(None, (profile.get("display_name"), profile.get("email")))),
        "页面: " + body.get("page", ""),
        "浏览器: " + body.get("client", ""),
    ]
    if body.get("project_id"):
        lines.append("故事: " + body["project_id"])
    if body.get("shot_id"):
        lines.append("镜头: " + body["shot_id"])
    return f"{label}[Inkseq 反馈] {category} {short}", "\n".join(lines)


def send(config, report, profile, environment, client=None):
    subject, text = compose(report, profile, environment)
    email = profile.get("email")
    (client or _client(config)).send_email(
        FromEmailAddress=config["sender"],
        Destination={"ToAddresses": [config["recipient"]]},
        # Replying to the email reaches the person who sent the report.
        **({"ReplyToAddresses": [email]} if email else {}),
        Content={
            "Simple": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": text, "Charset": "UTF-8"}},
            }
        },
    )
