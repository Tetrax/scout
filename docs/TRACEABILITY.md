# Traçabilité Scout Web 1.0

Cette matrice relie les intentions historiques aux mécanismes livrés et aux preuves reproductibles. Les documents historiques décrivent l’intention ; le code et les tests actuels décrivent le comportement exécutable.

| Idée initiale | Source documentaire | Réalisation 1.0 | Preuve automatisée / runtime |
|---|---|---|---|
| Découverte manuelle, 0..3 cartes, zéro valide | `docs/MVP_V1.md` | `ranking.MAX_CARDS`, `POST /discover`, aucune tâche planifiée | `test_zero_candidates_is_a_valid_result`, `test_discovery_is_bounded_*` |
| Faits et provenance verrouillés | `docs/MVP_V1.md`, `docs/GATE1_SUMMARY.md` | parseurs stricts, URL et identité de source validées, faits immuables après première sélection, résumé séparé de la déduction | `tests/test_web_sources.py`, `test_source_facts_are_immutable_after_first_display`, liens HTTPS réels vérifiés par smoke |
| 👎 / ❤️ / ⭐, silence neutre, correction | `docs/MVP_V1.md` | table `reactions`, upsert/correction/suppression, ⭐ filtré comme favori | `test_reaction_can_be_corrected_*`, parcours E2E Web |
| Le feedback influence vraiment le classement | mission Web App | effets par thème `-2.5/+2/+4`, bornés puis intégrés au score | `test_feedback_changes_targeted_topic_ranking_and_silence_is_neutral` |
| Déjà-vu et répétition pénalisés | principes de sélectivité | items et mêmes actualités déjà montrés exclus, comptes des 20 cartes récentes par source | `test_seen_and_repeated_sources_are_penalized_*`, `test_story_seen_from_one_source_is_excluded_*` |
| Diversité et sérendipité | intention « pas Fortinet/Hermes-only » | première passe une source par carte ; place hors intérêt dominant si score ≥ 2 | tests ranking diversité et sérendipité |
| Sources core réelles et bornées | `docs/MVP_V1.md`, périmètre Gate 1 | Fortinet RSS, CISA KEV Fortinet, Hermes Releases ; Codex Releases remplace X | `test_exactly_four_bounded_public_sources_*`, collecte préparatoire production |
| X read-only indisponible sans mutation | note projet / diagnostic du 2026-08-29 | source absente des fetchs, diagnostic visible, aucun token ou checkpoint X | contrat `X_BOOKMARKS_DIAGNOSTIC`, inspection UI |
| Dates absentes restent absentes ; vieux ≠ récent | mission Web App | `published_at=None` conservé, fenêtre de 180 jours et libellé explicite | `test_fortinet_rss_keeps_*`, `test_stale_content_is_not_presented_as_recent_*` |
| Collecte sûre et bornée | mission Web App | destinations fixes, no-redirect, limites octets/items/délai, cache, erreurs isolées | `tests/test_web_sources.py`, `tests/test_web_service.py` |
| Anti-lancements simultanés | mission Web App | singleton transactionnel SQLite avec expiration | `test_discovery_lock_rejects_*`, `test_existing_lock_rejects_*` |
| Authentification et espace personnel | mission Web App | dérivé scrypt, sessions serveur hashées, sessions anonymes bornées, cookie strict, CSRF, rate-limit, trusted hosts | `tests/test_web_app.py`, smoke HTTPS réel |
| Secret récupérable hors Git | mission Web App | bootstrap no-clobber, fichiers 700/600 sous `~/.config/scout`, valeurs transmises littéralement via `env_file.format: raw` | `tests/test_web_credentials.py`, `tests/test_web_deployment.py`, `stat` de production |
| Persistance SQLite simple | mandat Web App | schéma version 1, WAL, transactions, bind mount unique | `tests/test_web_database.py`, smokes redémarrage/redéploiement |
| Sauvegarde/restauration/rollback | mandat livraison | Online Backup, integrity check, publication atomique, restore service arrêté | `tests/test_web_maintenance.py`, runbook `docs/OPERATIONS.md` |
| Pas de faux apprentissage IA | contrainte modèle | statut visible `DETERMINISTIC_DEGRADED`, aucun secret Hermes monté | tests du run, inspection image/Compose et UI |
| Service production observable | mission déploiement | Gunicorn sans control socket sur rootfs read-only, healthcheck, restart policy, révision exacte, logs bornés | build et smoke Compose CI, health Docker, `/healthz`, inspect `restarts=0` |
| Responsive et noindex | mission produit | CSS mobile-first, CSP et en-têtes, robots globalement interdit | browser QA desktop/mobile, headers et `robots.txt` |

## Commande de gate locale

```bash
umask 077
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check scout_web scripts/prefetch.py tests/test_web_*.py wsgi.py
python3 -m compileall -q scout_mvp scout_web scripts tests wsgi.py
docker compose --env-file .env.example config --quiet
docker build --build-arg SCOUT_REVISION=verification -t scout:verification .
```

Les preuves finales de livraison sont le SHA fusionné sur `origin/main`, les checks GitHub conclusifs, le tag/ID d’image correspondant, l’état Docker `healthy` avec zéro redémarrage et les smokes navigateur sur `https://scout.valdev.me`.
