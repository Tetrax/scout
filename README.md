# Scout

Scout est une application personnelle de découverte volontaire : elle collecte des sources fixes et attribuables, puis propose de zéro à trois cartes. Zéro est un résultat valide ; la qualité et le coût d’attention priment sur le volume.

La Web App 1.0 complète la tranche CLI historique sans la supprimer. Les modules `scout_mvp/` restent disponibles pour les contrats fact-lockés et le parcours expérimental Hermes ; la production `scout.valdev.me` utilise la couche compacte `scout_web/`, déterministe et sans secret Hermes dans le conteneur.

## Parcours livré

- authentification serveur et sessions révocables stockées par dérivé SHA-256 ;
- lancement manuel d’une collecte bornée ;
- zéro à trois cartes réelles, sans remplissage artificiel ;
- résumé fidèle à la source, URL canonique et date uniquement quand la source la fournit ;
- raison de classement explicitement qualifiée d’« appréciation personnalisée (déduction) » ;
- réactions corrigibles : 👎 réduit les thèmes proches, ❤️ les renforce, ⭐ les renforce davantage et crée un favori ;
- historique, favoris et centres d’intérêt modifiables ;
- pénalité déjà-vu, pénalité de répétition, diversification des sources et place de sérendipité quand un candidat de qualité existe.

## Sources

| Source | Canal fixe | Limite par collecte |
|---|---|---:|
| Fortinet PSIRT | RSS FortiGuard officiel | 8 |
| CISA KEV · Fortinet | catalogue JSON officiel, filtré Fortinet | 8 |
| Hermes Agent | GitHub Releases officiel | 5 |
| OpenAI Codex | GitHub Releases officiel | 5 |

X Bookmarks reste désactivée et diagnostiquée (`unauthorized_client` observé le 2026-08-29). Scout ne modifie aucun accusé de lecture ou checkpoint X. OpenAI Codex est le remplacement public, gratuit et borné retenu.

Chaque destination réseau est codée en dur et vérifiée. Les redirections sont refusées, les réponses et délais sont bornés, les formats sont validés strictement et aucun URL fourni par l’utilisateur n’est récupéré.

## Architecture production

```text
Navigateur
  → Nginx :443 (TLS, noindex, en-têtes de sécurité)
  → 127.0.0.1:13739
  → Gunicorn / Flask non-root, root filesystem en lecture seule
  → SQLite WAL sur un unique bind mount persistant
       ↘ quatre sources HTTPS fixes, uniquement lors d’un lancement manuel
```

Le conteneur ne reçoit ni installation, ni OAuth, ni configuration Hermes. `MODEL_STATUS=DETERMINISTIC_DEGRADED` est volontaire et visible : le ranking utile fonctionne sans prétendre qu’un modèle serveur apprend. Voir [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Développement

Python 3.11 et 3.12 sont supportés.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --requirement requirements-dev.txt
umask 077
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check scout_web scripts/prefetch.py tests/test_web_*.py wsgi.py
python3 -m compileall -q scout_mvp scout_web scripts tests wsgi.py
SCOUT_ENV_FILE=.env.example docker compose --env-file .env.example config --quiet
```

Construire l’image :

```bash
docker build --build-arg SCOUT_REVISION="$(git rev-parse HEAD)" -t "scout:$(git rev-parse HEAD)" .
```

`requirements.txt` expose les dépendances directes ; `requirements.lock` fige l’environnement de production ; `requirements-dev.txt` ajoute uniquement les outils de développement.

## Exploitation

Le déploiement utilise `compose.yaml`, un tag d’image immuable, un port loopback et des secrets créés une seule fois hors Git. Le runbook détaillé couvre bootstrap, déploiement, healthcheck, collecte préparatoire, sauvegarde, restauration et rollback : [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

Chemins de production retenus :

```text
/home/tetrax/.config/scout/scout.env       # dérivés et clé serveur, mode 600
/home/tetrax/.config/scout/access.txt      # récupération initiale, mode 600
/home/tetrax/.config/scout/deployment.env  # image/révision/port/chemins, mode 600
/home/tetrax/.local/state/scout/web/       # SQLite et sauvegardes, mode 700
```

Ne jamais committer ou afficher le contenu de ces fichiers. La commande de bootstrap n’imprime que leurs chemins :

```bash
umask 077
sudo -u tetrax env PYTHONPATH="$PWD" "$PWD/.venv/bin/python" -m scout_web.credentials \
  --directory /home/tetrax/.config/scout \
  --url https://scout.valdev.me \
  --username valentin
```

## Documentation

- [`docs/MVP_V1.md`](docs/MVP_V1.md) : intention et contraintes initiales ;
- [`docs/GATE1_SUMMARY.md`](docs/GATE1_SUMMARY.md) : synthèse publique du Gate 1 historique ;
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) : architecture et frontières de confiance ;
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) : runbook production et rollback ;
- [`docs/TRACEABILITY.md`](docs/TRACEABILITY.md) : idée initiale → source documentaire → réalisation → preuve.

## Frontière du dépôt

Le dépôt ne contient que code, tests, schémas, documentation, CI et exemples neutres. Données personnelles, sessions, réactions, favoris, secrets, caches et sauvegardes restent hors Git. Aucun crawler permanent, cron, notification, envoi tiers ou contenu de démonstration n’est activé.
