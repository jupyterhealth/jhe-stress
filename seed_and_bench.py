"""
Expected to run inside via django `manage.py shell`

e.g.

cat seed_and_bench.py | kubectl exec -i -n staging deployments/staging-jhe -c jhe -- python3 manage.py shell --no-imports -i python
"""

import json
import secrets
import sys
import time
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass
from functools import partial, wraps
from pathlib import Path
from subprocess import check_output

import requests
from core.models import (
    CodeableConcept,
    JheUser,
    Observation,
    Organization,
    PatientOrganization,
    Study,
    StudyPatient,
    StudyPatientScopeConsent,
    StudyScopeRequest,
)
from core.utils import generate_observation_value_attachment_data
from django.conf import settings
from django.utils import timezone
from oauth2_provider.oauth2_validators import OAuth2Validator
from oauthlib.common import Request

t = int(time.time())
user = JheUser.objects.create_user(
    email=f"bench-{t}@example.org",
    user_type="practitioner",
    first_name="Test",
    last_name=f"User {t}",
)
access_token = secrets.token_urlsafe(32)
oauth_request = Request("")
oauth_request.user = user
validator = OAuth2Validator()
validator.save_bearer_token(
    {
        "access_token": access_token,
        "scope": "openid",
    },
    oauth_request,
)
s = requests.Session()
s.headers = {"Authorization": f"Bearer {access_token}"}

base_url = settings.SITE_URL
r = s.get(f"{base_url}/api/v1/users/profile")
print(r.json(), file=sys.stderr)

root_org = Organization.objects.get(id=0)
organization = Organization.objects.create(
    name="Profile",
    type="other",
    part_of=root_org,
)
user.practitioner.organizations.add(organization)

study = Study.objects.create(
    name="Demo Study",
    description="Sample data for benchmarking",
    organization=organization,
)
code, _ = CodeableConcept.objects.update_or_create(
    coding_system="https://w3id.org/openmhealth",
    coding_code="omh:heart-rate:2.0",
    text="Heart Rate (OMH)",
)
StudyScopeRequest.objects.create(study=study, scope_code=code)


def add_patient():
    n = JheUser.objects.all().count()
    user = JheUser.objects.create_user(
        email=f"patient-{t}-{n}@example.com",
        user_type="patient",
    )
    patient = user.patient
    PatientOrganization.objects.create(patient=patient, organization=organization)
    _study_patient = StudyPatient.objects.create(study=study, patient=patient)
    study_patient = StudyPatient.objects.get(study=study, patient=patient)

    StudyPatientScopeConsent.objects.create(
        study_patient=study_patient,
        scope_code=code,
        consented=True,
        consented_time=timezone.now(),
    )
    return patient


patient = add_patient()


def add_records(n_records):
    observations = []

    starting_attachment = generate_observation_value_attachment_data(code.coding_code)
    for i in range(n_records):
        attachment = deepcopy(starting_attachment)
        attachment["header"]["uuid"] = str(uuid.uuid4())
        observations.append(
            Observation(
                subject_patient=patient,
                codeable_concept=code,
                status="final",
                omh_data=attachment,
            )
        )
    Observation.objects.bulk_create(observations)


def get_paginated_admin(url):
    r = s.get(url)
    if r.status_code >= 300:
        print(r.text)
    r.raise_for_status()
    page = r.json()
    results = page["results"]
    next_url = page.get("next")
    while next_url and page["results"]:
        r = s.get(next_url)
        r.raise_for_status()
        page = r.json()
        results.extend(page["results"])
        next_url = page.get("next")
    return len(results)


def _get_link(bundle, rel):
    """Get link from FHIR link list"""
    for link in bundle["link"]:
        if link["relation"] == rel:
            return link["url"]
    return None


def get_paginated_fhir(url):
    r = s.get(url)
    if r.status_code >= 300:
        print(r.text)
    r.raise_for_status()
    page = r.json()
    results = page["entry"]
    next_url = _get_link(page, "next")
    while next_url and page["entry"]:
        r = s.get(next_url)
        r.raise_for_status()
        page = r.json()
        results.extend(page["entry"])
        next_url = _get_link(page, "next")
    return len(results)


def cleanup_after(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        finally:
            # delete everything
            for sp in StudyPatient.objects.filter(study=study):
                sp.patient.delete()
            # study.delete()
            organization.delete()
            user.practitioner.delete()
            user.delete()
            print(Observation.objects.all().count())

    return wrapped


def measure(f):
    tic = time.perf_counter()
    result = f()
    toc = time.perf_counter()
    return toc - tic, result


@dataclass
class Measurement:
    api: str
    per_page: int
    t: float
    n: int


def save_measurements(measurements, fname):
    with Path(fname).open("w") as f:
        json.dump([asdict(m) for m in measurements], f)


@cleanup_after
def main(dest):
    running_count = 0
    per_patient = 1000
    n_list = [1]
    n_list.extend(range(10, 100, 10))
    n_list.extend(range(100, 1000, 100))
    n_list.extend(range(1000, 10_001, 1000))
    # n_list.extend(range(10_000, 100_001, 10_000))

    measurements = []
    for N in n_list:
        while running_count < N:
            delta = min(N - running_count, per_patient)
            running_count += delta
            add_records(delta)
        per_page_list = [20, 100, 1000]
        last_times = {
            "admin": {n: 0 for n in per_page_list},
            "fhir": {n: 0 for n in per_page_list},
        }
        for per_page in per_page_list:
            # if per_page > 20 and per_page > N:
            #     continue
            if last_times["admin"][per_page] < 60:
                t, count = measure(
                    partial(
                        get_paginated_admin,
                        f"{base_url}/api/v1/observations?pageSize={per_page}&patient_id={patient.id}",
                    )
                )
                last_times["admin"][per_page] = t
                if count != N:
                    print(f"wrong! {count=}, {N=}", file=sys.stderr)
                m = Measurement(t=t, api="admin", per_page=per_page, n=running_count)
                print(m, file=sys.stderr)
                measurements.append(m)
            if last_times["fhir"][per_page] < 60:
                t, count = measure(
                    partial(
                        get_paginated_fhir,
                        f"{base_url}/fhir/r5/Observation?_count={per_page}&patient={patient.id}",
                    )
                )
                last_times["fhir"][per_page] = t
                if count != N:
                    print(f"wrong! {count=}, {N=}")
                m = Measurement(t=t, api="fhir", per_page=per_page, n=running_count)
                print(m)
                measurements.append(m)
                save_measurements(measurements, dest)


if __name__ in {"__main__", "django.core.management.commands.shell"}:
    try:
        rev = check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
        ).strip()
    except Exception as e:
        print(f"Failed to get git rev: {e}", file=sys.stderr)
        rev = str(int(time.time()))
    main(f"{rev}.json")
