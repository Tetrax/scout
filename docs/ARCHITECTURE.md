# Architecture de Scout Web 1.0

## Objectif

Scout réduit volontairement le volume : un lancement manuel produit de zéro à trois cartes réelles. L’application est personnelle, compréhensible, persistante et exploitable sur un petit VPS sans introduire de file de messages, de service de modèles ou de crawler permanent.

## Composants

- `scout_web/sources.py` : catalogue fermé de quatre sources, récupération HTTPS bornée, validation et normalisation ;
- `scout_web/database.py` : schéma SQLite versionné, transactions, WAL, intérêts, items, réactions, runs, cache, verrou et sessions ;
- `scout_web/ranking.py` : score déterministe, explicable, diversification et sérendipité ;
- `scout_web/service.py` : collecte concurrente, cache, isolation des pannes, verrou anti-concurrence et publication d’un run ;
- `scout_web/auth.py` : sessions serveur, CSRF et rate-limit de connexion ;
- `scout_web/app.py` : routes Flask, validation des entrées, politiques HTTP et rendu Jinja échappé ;
- `scout_web/maintenance.py` : sauvegarde SQLite en ligne, vérification d’intégrité et restauration atomique à service arrêté ;
- `scout_web/credentials.py` : création unique des secrets et du fichier de récupération privé ;
- `wsgi.py` : point d’entrée Gunicorn ;
- `compose.yaml` : limites runtime, utilisateur non-root, rootfs read-only, volume persistant et healthcheck.

## Flux de données

1. Le navigateur authentifié soumet `POST /discover` avec un jeton CSRF.
2. Une ligne SQLite singleton refuse un second lancement concurrent et expire après une durée bornée.
3. Les sources dues sont collectées en parallèle. Chaque worker ne connaît qu’une `SourceDefinition` issue du catalogue fermé.
4. Le parseur valide la taille, le format, l’identité, le lien canonique, les dates et les champs textuels. Les pages et descriptions sont des données hostiles ; elles ne deviennent jamais des instructions.
5. SQLite déduplique par `(source_id, external_id)`, URL et `story_key`.
6. Le ranking exclut le déjà-vu, filtre les dates hors fenêtre, combine qualité de source, fraîcheur, intérêt explicite, effets des réactions et répétition récente.
7. La sélection préfère des sources distinctes et réserve, si possible, une place de sérendipité hors thèmes dominants.
8. Le run et ses cartes sont écrits transactionnellement, puis consultables dans l’historique.

## Frontières de confiance

### Sources externes

Les quatre URLs de récupération sont constantes. Les redirections sont bloquées ; schéma, hôte et route des liens visibles sont validés ; les tailles et délais sont plafonnés. Il n’existe aucun fetch générique ni champ URL utilisateur : la surface SSRF reste fermée.

Les faits source conservés sont : titre, URL, date réellement fournie, résumé/extrait, provenance et identité. Le texte de justification est calculé séparément et commence par `Appréciation personnalisée (déduction)`.

Un objet encore seulement en cache peut être rafraîchi. Dès sa première sélection dans `run_items`, ses faits deviennent immuables : les collectes suivantes ne mettent à jour que `last_collected_at`. L’historique ne peut donc pas changer rétroactivement avec la source.

### Authentification

Le mot de passe n’est conservé que comme dérivé Werkzeug `scrypt`. Le token de session aléatoire n’est conservé qu’en SHA-256 ; le cookie est `Secure`, `HttpOnly`, `SameSite=Strict`. Les mutations exigent un jeton CSRF serveur. Une connexion réussie révoque la session anonyme et en crée une nouvelle. Les échecs sont limités par IP dérivée au HMAC.

Nginx est le seul client direct du backend loopback. `ProxyFix(x_for=1, x_proto=1)` suppose exactement ce proxy de confiance. Les hôtes acceptés sont limités à `scout.valdev.me`, `localhost` et `127.0.0.1`.

### Secrets

Le conteneur ne monte aucun fichier Hermes, OAuth, SSH ou GitHub. Seuls le nom d’utilisateur, le dérivé du mot de passe et la clé de session Scout lui sont fournis. Le mot de passe initial en clair n’existe que dans `/home/tetrax/.config/scout/access.txt`, lisible par `tetrax` uniquement.

### Modèle

Aucun modèle serveur n’est connecté dans la version 1.0. Le statut explicite `DETERMINISTIC_DEGRADED` évite de confondre le quota OAuth interactif Hermes avec une API de production. L’ancienne tranche CLI `scout_mvp/` et son triage borné demeurent distincts ; ils ne sont pas invoqués par la Web App.

## Persistance et cohérence

Le schéma courant est `PRAGMA user_version=1`. La base utilise les clés étrangères, `busy_timeout=10000` et le journal WAL. Le bind mount `/var/lib/scout` est la seule zone d’écriture durable du conteneur.

La sauvegarde utilise l’API SQLite Online Backup et vérifie `PRAGMA integrity_check` avant publication atomique. Une restauration refuse un snapshot corrompu, exige une confirmation explicite que le service est arrêté, écrit un fichier privé temporaire, vérifie son intégrité, puis le publie atomiquement.

## Contraintes opérationnelles

- un worker Gunicorn et quatre threads : SQLite reste simple et l’accès est personnel ;
- collecte réseau maximale théorique : quatre requêtes en parallèle, délai individuel de 12 s ;
- cache réussi : 30 min ; retry d’une source en erreur : 5 min ;
- taille maximale d’une requête applicative : 64 KiB ;
- aucun scheduler, notification ou webhook ;
- aucun remplissage synthétique si aucune carte n’est éligible.
