# Recursos API

API REST (FastAPI + SQLite) per a la PWA del Banc de recursos. Substitueix
Firebase (Firestore + Authentication, projecte `edubibliojocs`) des de 2026-09.

Usa `edutictac-community` com a nucli comú per a connexió SQLite, rate limit,
cookies firmades, client OIDC i router de comunitat. La lògica pròpia del Banc
de recursos (índex federat i integracions educatives) continua en aquest servei.

## Endpoints

- `GET  /api/health`
- `GET  /api/preferences` - favorits, valoracions, avisos, resums agregats i flag admin.
- `GET  /api/teacher/favorites` - recursos complets favorits del docent autenticat.
- `POST /api/teacher/documents` - puja un document privat del docent (PDF, Word,
  ODT, presentacions, fulls de càlcul o TXT; màxim 25 MB).
- `GET  /api/documents/{id}/download` - serveix un document pujat per a una
  assignació d'aula.
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
PLAY_INGEST_TOKEN=...
PLAY_ALLOWED_HOSTS=edutictac.es,play.edutictac.es
```

Els secrets d'OIDC i sessió es guarden fora del repositori, en el fitxer
d'entorn del servei.

En un aula Commons que publica Play per HTTP, configura també `PLAY_PUBLIC_URL`
amb l'adreça real (per exemple `http://edutictac.local:8084`) i inclou el seu
host en `PLAY_ALLOWED_HOSTS`. Només s'admet HTTP per eixe origen exacte
(host i port), a més de la compatibilitat local amb localhost/127.0.0.1.
Sense esta configuració es continua exigint HTTPS als altres hosts.

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


## Publicacions de Llibres

`app/books.py` reutilitza la mateixa SQLite: fitxa `Resource`, metadades i fitxers
públics s'actualitzen en una transacció. No es necessita un servei nou.
`GET /api/llibres/books` i `/api/llibres/public/{id}/{path}` són públics.
`/api/internal/llibres/{draft}` exigeix el token d'aplicació existent i la identitat
individual proporcionada pel proxy de Commons; no s'ha de cridar des del navegador.
El servidor genera HTML sense JavaScript a partir de pàgines SVG validades i PNG
normalitzats. L'editable `.elpx` és un adjunt opcional; no s'executa.

Variables: `LLIBRES_PUBLIC_BASE_URL` (origen HTTP/S públic del banc, sense ruta) i
`LLIBRES_PUBLIC_MAX_BYTES` (quota global, 2 GiB per defecte). El volum `/data`
existent conserva tot el contingut en `recursos.db`; incloure'l en les còpies de
seguretat mitjançant SQLite backup. Retirar elimina els BLOB públics i desactiva la
fitxa. El codi corresponent es descarrega en `/api/llibres/source.zip`.

El contracte complet i l'activació estan documentats en
`edutictac-llibres/docs/RESOURCE_PUBLICATION.md`. Proves: `pytest -q tests/test_books.py`.

Les revisions de l’alumnat es guarden privades en `llibres_submissions` i
`llibres_review_files`, en la mateixa SQLite. L’aprovació publica la còpia exacta
i registra la decisió en una transacció. `app/book_reviews.py` només accepta la
identitat i l’aula verificades pel proxy de Commons. La quota inclou fitxers
públics i privats. Proves addicionals: `pytest -q tests/test_book_reviews.py`.

## Catàleg local i exportació interna

Amb `EDUHOOT_LOCAL_CATALOG=1`, les activitats d’EduHoot es consulten al servei
local i no es reutilitzen els identificadors del catàleg públic descarregat.
La deduplicació usa el proveïdor i l’identificador, conservant altres recursos.

`POST /api/internal/export-catalog` regenera `games.json` i `games-home.json`
a partir de l’índex actual, incloses les etiquetes. Requerix l’autenticació amb
`RESOURCES_IMPORT_TOKEN` i una carpeta de destí configurada al servidor amb
`RECURSOS_EXPORT_DIR`; el client no pot triar la ruta. Sense configuració retorna
503 i sense autenticació vàlida retorna 401, sense escriure fitxers.

Proves: `pytest -q tests/test_local_eduhoot_catalog.py tests/test_catalog_export.py`.

## Trasllat de llibres públics entre instal·lacions locals

`scripts/migrate-public-books.py` trasllada únicament publicacions actives, les
seues fitxes i els fitxers del lector. No copia propostes privades, credencials
o preferències, ni substituïx altres recursos. Cal actualitzar primer la API de
destí i fer una còpia consistent de la SQLite amb permisos restringits.

```sh
python3 scripts/migrate-public-books.py --source /origen/recursos.db \
  --target /desti/recursos.db --public-base http://banc.local:8086
# Afegir --apply únicament després de revisar la simulació i la còpia de seguretat.
```

La simulació és el comportament per defecte. Els identificadors i revisions es
conserven; només s'adapta l'origen dels enllaços del lector. Repetir el trasllat
no duplica els llibres idèntics. Una revisió o fitxa diferent en destí atura tota
l'operació, sense sobreescriure-la. Les insercions es fan en una transacció i no
s'extrauen fitxers al sistema de fitxers. Es recomana fer-ho amb les publicacions
aturades; una publicació nova en origen requerirà una altra revisió de la còpia.

Proves: `pytest -q tests/test_public_book_migration.py`.

La ingesta autenticada de Play admite sus 13 tipos actuales, incluido Modo Reto.
Esto solo amplía el catálogo de tipos: se mantienen la autenticación de servidor,
la validación de origen, la visibilidad pública y la publicación idempotente.
La aprobación docente de actividades del alumnado se autoriza en Commons/Play;
el banco recibe únicamente publicaciones ya autorizadas por ese servicio.

El compilador de publicaciones de Llibres admite `es`, `ca`, `va`,
`ca-valencia` (compatibilidad) y `en`. Conserva el idioma en el catálogo y el
atributo `lang` del lector HTML, normalizando únicamente `va` a `ca-valencia`.
El lector inglés traduce sus controles de créditos y descarga. Los valores no
admitidos siguen rechazándose antes de publicar. No requiere migración de datos.

Producción: perfil Docker, preservación de datos, desactivación de datos de
prueba (`RECURSOS_SEED=0`), conexión con Commons y reversión en
[PRODUCTION_LLIBRES.md](docs/PRODUCTION_LLIBRES.md).
