# Recursos API

API REST (FastAPI + SQLite) per a la PWA del Banc de recursos. Substitueix
Firebase (Firestore + Authentication, projecte `edubibliojocs`) des de 2026-09.

Usa `edutictac-community` com a nucli comú per a connexió SQLite, rate limit,
cookies firmades, client OIDC i router de comunitat. La lògica pròpia del Banc
de recursos (índex federat i integracions educatives) continua en aquest servei.

## Endpoints

- `GET  /api/health`
- `GET  /api/preferences` - favorits, valoracions, avisos, resums agregats i flag admin.
- `POST /api/favorites/toggle` `{ game_key }`
- `POST /api/ratings` `{ game_key, value }` (amb lògica de toggle 1-5)
- `POST /api/reports` `{ game_key }` (activitat trencada)
- `GET  /api/submissions`
- `POST /api/submissions` `{ title, url, notes, area, language, name }`
- `GET  /api/auth/login` - inicia sessió amb Authentik
- `GET  /api/auth/logout` - tanca sessió local
- `GET  /api/auth/me` - estat de sessió OIDC
- `POST /api/student/login` - inicia sessió amb credencial pseudònima EduTicTac ID
- `POST /api/student/logout` - tanca sessió d'alumnat
- `POST /api/teacher/student-batches` `{ count, pin_length }` - genera credencials pseudònimes per a professorat autenticat
- `GET  /api/teacher/student-identities/by-code/{public_code}` - localitza una credencial activa per regenerar el PIN
- `POST /api/teacher/activity-assignments` - crea una activitat proposada pel docent per separar resultats per context
- `GET  /api/teacher/activity-assignments` - llista activitats proposades pel docent
- `GET  /api/teacher/activity-assignments/{id}/results` - informe mínim de resultats per activitat proposada
- `POST /api/admin/resources/hide` `{ game_key }` - oculta un recurs del llistat públic
- `GET  /api/resources` - índex federat de recursos (cerca + filtres)
- `POST /api/integrations/recursos/activities/{id}/publish` - ingesta
  autenticada e idempotente d'activitats H5P públiques de Play
- `GET  /api/integrations/recursos/activities/{id}` - estat de sincronització
  d'una activitat de Play
- `POST /api/integrations/recursos/activities/{id}/unpublish` - retirada
  autenticada d'una activitat de Play
- `GET  /api/admin/sources` - estat de les fonts de recursos (requereix admin)

Identitat anònima per cookie firmada (HMAC, sense Google). El mode admin
s'activa en iniciar sessió amb Authentik si el correu OIDC figura en
`OIDC_ADMIN_EMAILS`. L'alumnat pot iniciar sessió amb una credencial pseudònima
EduTicTac ID (`public_code + PIN`); el backend només guarda la sessió local
`student:<codi>:<identity_id>` per a favorits, valoracions i avisos.

## Nucli comú

Dependència estable actual:

```txt
edutictac-community @ git+https://git.edutictac.es/Edutictac/edutictac-community.git@v0.1.4
```

Components reutilitzats:

- `edutictac_community.db.connect` per a SQLite amb WAL.
- `edutictac_community.ratelimit.RateLimiter` per a límits en memòria.
- `edutictac_community.session.SignedSession` per a cookies HMAC.
- `edutictac_community.oidc.OIDCClient` per a Authentik/OIDC.
- `edutictac_community.community.create_community_router` per a favorits,
  valoracions, avisos i ocultació admin, configurat amb `game_key`.

El router comú es munta amb `key_field="game_key"`,
`db_key_column="game_key"` i `admin_hide_path="/admin/resources/hide"`, de
manera que la PWA i les taules SQLite existents no canvien.

## Desplegament

Codi en `/opt/recursos-api`, servei systemd `recursos-api.service`
(uvicorn `127.0.0.1:8004`), exposat en `recursos.edutictac.es/api/`.
Variables d'entorn en `/etc/recursos-api.env`:

```txt
RECURSOS_SECRET=...
RECURSOS_DB=/var/lib/recursos-api/recursos.db
OIDC_ISSUER=https://id.edutictac.es/application/o/<slug>/
OIDC_CLIENT_ID=...
OIDC_CLIENT_SECRET=...
OIDC_REDIRECT_URI=https://recursos.edutictac.es/api/auth/callback
OIDC_SCOPE=openid
OIDC_ADMIN_SUBS=...
OIDC_ADMIN_EMAILS=...
EDUTICTAC_ID_API_URL=http://127.0.0.1:8005
EDUTICTAC_ID_TEACHER_TOKEN=...
```

Els secrets d'OIDC i sessió es guarden fora del repositori, en el fitxer
d'entorn del servei.

## Dades migrades

`seed.json` conté les dades exportades de Firestore el 2026-09-04:
`ratingSummary` (11), `brokenReports` (28) i `submissions` (1). S'insereixen en
la primera inicialització de la base de dades (si la taula està buida).

## Índex federat de recursos

A més de favorits/valoracions, aquest backend allotja l'índex federat de
recursos educatius oberts que alimentarà `recursos.edutictac.es`.

- Proveïdors: `jclic`, `h5p`, `scorm`, `eduhoot` (paquet `app/providers/`).
- Model comú `Resource` + `SyncRun` (SQLite, taules `resources` i `sync_runs`).
- CLI de sincronització:

```bash
python -m app.cli sync jclic|h5p|scorm|eduhoot|all
python -m app.cli sync scorm --url https://.../paquet.zip
python -m app.cli stats
python -m app.cli sources
```

- Cerca unificada en `GET /api/resources` amb filtres (`q`, `provider`,
  `format`, `subject`, `stage`, `language`, `license`, `license_known`).
- Play usa el proveïdor `edutictac-play` amb `external_id` estable
  `edutictac-play:{activity_id}`. La ingesta només s'activa si es configura
  `PLAY_INGEST_TOKEN`; el token viatja com `Authorization: Bearer ...`.
- Les activitats privades o de classe es rebutgen. La retirada només marca el
  recurs com a inactiu, de manera que els favorits i l'històric no es perden.
- Documentació completa i fonts verificades: vegeu
  `docs/resource-indexers.md` en el repositori del frontend (`sasogu/recursos`).

Tests: `python -m pytest tests/` (requereix `httpx`, `defusedxml`, `pytest`).

## Llicència

GNU Affero General Public License v3.0 (AGPL-3.0).
