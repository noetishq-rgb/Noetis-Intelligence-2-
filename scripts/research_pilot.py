"""Capture public pilot evidence or inspect config. Never dispatches a paid model call."""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from dotenv import dotenv_values
from pydantic import Field, HttpUrl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.models import StrictModel, ShortText, Text, Citation
from app.research_models import SourceSeed
from app.services.research_sources import collect


class PilotManifest(StrictModel):
    brand_name: ShortText
    website_url: HttpUrl
    purpose: Text
    sources: list[SourceSeed] = Field(min_length=1, max_length=8)
    limitations: list[Text]


def preflight():
    local = dotenv_values(ROOT / '.env')
    def value(key):
        return os.environ.get(key, local.get(key)) or ''
    enabled = value('MI_ALLOW_MODEL_CALLS').lower() in {'true', '1'}
    provider = value('MI_MODEL_PROVIDER') == 'anthropic'
    key = value('MI_ANTHROPIC_API_KEY')
    key_present = bool(key and not key.startswith('REPLACE_'))
    model_present = bool(value('MI_ANTHROPIC_MODEL'))
    ready = enabled and provider and key_present and model_present
    return {'configuration_only': True, 'network_calls': 0, 'model_calls': 0,
        'model_calls_enabled': enabled, 'supported_provider_selected': provider,
        'provider_key_present': key_present, 'model_configured': model_present,
        'provider_configuration_ready': ready, 'live_provider_verified': False,
        'requires_confirmed_brand_intake': True,
        'next_step': 'Use the authenticated research API and dedicated worker for a bounded live pilot.' if ready
            else 'Configure server-only provider enable flag, provider, API key and model before a live pilot.'}


def capture(manifest_path, output_dir):
    manifest = PilotManifest.model_validate_json(Path(manifest_path).read_text())
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise ValueError('Use an empty output directory to preserve previous captures')
    traces = []
    sources, limitations = collect(manifest.sources, [], trace=traces.append)
    snapshots = []
    captured = datetime.now(timezone.utc)
    for source in sources:
        sid = uuid4()
        citation = Citation(source_id=sid, url=source.url, publisher=source.publisher,
            published_at=source.published_at, accessed_at=captured,
            geographic_scope=source.seed.geographic_scope, applicability_to_india=source.seed.applicability_to_india,
            access_limitations=source.limitations)
        snapshots.append({'id': str(uuid4()), 'citation': citation.model_dump(mode='json'),
            'area': source.seed.area, 'kind': source.seed.kind, 'confidence_cap': source.confidence_cap,
            'extracted_text': source.text, 'extracted_sha256': hashlib.sha256(source.text.encode()).hexdigest(),
            'raw_content_sha256': source.content_sha256, 'model_excerpt': source.text[:2400]})
    result = {'brand_name': manifest.brand_name, 'website_url': str(manifest.website_url),
        'captured_at': captured.isoformat(), 'status': 'evidence_captured' if snapshots else 'no_accessible_evidence',
        'accepted_sources': len(snapshots), 'source_limitations': manifest.limitations + limitations,
        'model_calls': 0, 'dossier_generated': False, 'human_approval': False,
        'next_step': ('Confirm brand intake, then run the authenticated research worker once provider configuration is ready.'
            if snapshots else 'Resolve public-source capture access before model generation; confirm intake and server provider configuration.')}
    (target / 'sources.json').write_text(json.dumps(snapshots, indent=2, ensure_ascii=False)+'\n')
    (target / 'capture-report.json').write_text(json.dumps(result, indent=2, ensure_ascii=False)+'\n')
    (target / 'tool-traces.json').write_text(json.dumps(traces, indent=2)+'\n')
    (target / 'provider-readiness.json').write_text(json.dumps(preflight(), indent=2)+'\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('preflight')
    cap = sub.add_parser('capture')
    cap.add_argument('--manifest', required=True)
    cap.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'preflight':
        result = preflight()
    else:
        result = capture(args.manifest, args.output)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
