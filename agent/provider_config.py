"""Load provider plans and explicitly import literal secrets without shell execution."""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit
import yaml
PROVIDER_ENV = {"nvidia": "NVIDIA_API_KEY", "modelscope": "MODELSCOPE_API_KEY",
                "zhipu": "ZHIPU_API_KEY", "agnes": "AGNES_API_KEY"}
PROVIDER_HOST = {"nvidia": "integrate.api.nvidia.com", "modelscope": "api-inference.modelscope.cn",
                 "zhipu": "open.bigmodel.cn", "agnes": "apihub.agnes-ai.com"}

def parse_provider_secrets(text: str) -> dict[str, str]:
    result = {}
    for number, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*", line)
        if not match or match[1] not in PROVIDER_ENV.values() or match[1] in result:
            raise ValueError(f"Invalid or duplicate secret assignment at line {number}")
        name, value = match.groups()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"Invalid quoted secret at line {number}")
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if not value or not re.fullmatch(r"[A-Za-z0-9._+/=-]+", value):
            raise ValueError(f"Non-literal secret value at line {number}")
        result[name] = value
    return result

def import_provider_secrets(text: str, vault) -> list[str]:
    values = parse_provider_secrets(text)
    if set(PROVIDER_ENV.values()) - set(values):
        raise ValueError("Provider secret file must contain all four supported variables")
    for provider, variable in PROVIDER_ENV.items():
        vault.set(f"{provider}_api_key", values[variable])
    return list(PROVIDER_ENV)

def _reject_inline_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {"api_key", "secret", "password", "access_token", "token"}:
                raise ValueError("Provider plan must use credential references, not inline secrets")
            _reject_inline_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _reject_inline_secrets(item)

def validate_provider_plan(plan: dict) -> dict:
    if not isinstance(plan, dict) or not isinstance(plan.get("providers"), dict) or not isinstance(plan.get("roles"), dict):
        raise ValueError("Provider plan requires providers and roles mappings")
    _reject_inline_secrets(plan)
    for name, provider in plan["providers"].items():
        if name not in PROVIDER_ENV or not isinstance(provider, dict):
            raise ValueError("Unsupported provider configuration")
        url = urlsplit(str(provider.get("base_url", "")))
        if (url.scheme != "https" or url.hostname != PROVIDER_HOST[name] or url.username
                or url.password or url.query or url.fragment or url.port not in (None, 443)):
            raise ValueError(f"Provider endpoint does not match credential owner: {name}")
        if provider.get("api_key_env") != PROVIDER_ENV[name]:
            raise ValueError(f"Provider credential reference mismatch: {name}")
    for role in ("main_brain", "sub_agent", "code", "vision", "memory_embedding"):
        config = plan["roles"].get(role)
        chain = config.get("chain") if isinstance(config, dict) else config
        if not isinstance(chain, list) or not chain:
            raise ValueError(f"Missing model chain: {role}")
        for entry in chain:
            if not isinstance(entry, str) or "@" not in entry:
                raise ValueError(f"Invalid model reference in role: {role}")
            model, provider = entry.rsplit("@", 1)
            if not model or provider not in plan["providers"]:
                raise ValueError(f"Unknown provider in role: {role}")
    return plan

def load_provider_config(cfg: dict, config_path: str | Path) -> dict:
    routing = cfg.get("llm_router", {})
    if not isinstance(routing, dict) or routing.get("enabled") is not True:
        return cfg
    merged = copy.deepcopy(cfg)
    if "providers_file" in routing:
        source = Path(routing["providers_file"])
        if not source.is_absolute():
            source = Path(config_path).resolve().parent / source
        if not source.is_file():
            raise ValueError("Configured provider plan file is missing")
        plan = validate_provider_plan(yaml.safe_load(source.read_text(encoding="utf-8-sig")))
        merged["llm_router"] = {**plan, **routing}
    validate_provider_plan(merged["llm_router"])
    return merged

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--env-file", type=Path)
    sources.add_argument("--git-ref")
    parser.add_argument("--vault-dir", type=Path, default=Path("vault"))
    args = parser.parse_args(argv)
    try:
        if args.env_file:
            text = args.env_file.read_text(encoding="utf-8-sig")
        else:
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", args.git_ref):
                raise ValueError("Invalid git ref")
            result = subprocess.run(["git", "show", args.git_ref + ":docs/model-api-guide/llm_secrets.env"],
                                    capture_output=True, check=False)
            if result.returncode:
                raise ValueError("Specified secret file is unavailable")
            text = result.stdout.decode("utf-8-sig")
        from vault import Vault
        providers = import_provider_secrets(text, Vault.for_machine(Path(args.vault_dir).parent))
        print(json.dumps({"imported_providers": providers, "storage": "vault", "values_printed": False}))
        return 0
    except Exception as exc:
        print(json.dumps({"success": False, "error_type": type(exc).__name__}))
        return 2
if __name__ == "__main__":
    raise SystemExit(main())
