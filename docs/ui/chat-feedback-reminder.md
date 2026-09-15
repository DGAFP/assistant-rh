# Rappel d’évaluation avant de quitter un chat

Le rappel de l’issue #564 s’applique aux deux boutons « New chat » (en-tête
et volet gauche) et au changement de ministère.

## Règle d’évaluation

Les avis existants portent sur une **réponse**, pas sur une conversation.
Une conversation est considérée comme évaluée dès qu’au moins un de ses tours
possède un `feedback` enregistré, y compris une note minimale ou un avis négatif.
Une sélection d’étoiles non envoyée ne suffit pas. Ajouter une nouvelle réponse
à une conversation déjà évaluée ne réactive pas le rappel.

En l’absence d’avis, le rappel cible la dernière réponse non vide autorisée par
le même filtre `is_negative_response` que le formulaire actuel. Ce filtre accepte
actuellement toutes les réponses ; les réponses sans sources restent donc évaluables.
Une conversation vide ou contenant seulement des réponses vides ne déclenche rien.

## Transition et sauvegarde

- La demande de sortie est conservée en session. Le chat, son identifiant et le
  ministère restent actifs ; le sélecteur affiche immédiatement le ministère d’origine.
- « Évaluer puis continuer » affiche la dernière réponse et son formulaire habituel :
  étoiles de 1 à 5, raisons/commentaire et validation inchangés (ou pouces si V1 activée).
  L’action de sortie s’exécute après l’envoi réussi de ce formulaire.
- L’avis conserve le `turn_id`, le `turn_idx` et le `session_id` d’origine. Le ministère
  et la conversation restent ceux du `chat_runs` lié par `turn_id` : aucune migration
  ni nouveau champ d’évaluation n’est nécessaire.
- Le mécanisme PostgreSQL avec secours CSV est conservé. L’avis est marqué envoyé
  seulement après le retour réussi du writer. Si la sauvegarde échoue, le formulaire
  affiche une erreur et conserve le chat ; l’utilisateur peut réessayer ou choisir
  explicitement « Continuer sans évaluer ».
- « Continuer sans évaluer » exécute une seule fois la sortie sans créer d’avis.
- « Annuler », la croix, Échap ou un clic hors de la fenêtre abandonnent la sortie.
- Les étoiles, raisons et commentaires non envoyés sont conservés pendant le
  rappel, puis retrouvés dans le dialogue d’évaluation ou après annulation.

La nouvelle conversation reçoit un nouvel identifiant et de nouvelles suggestions.
La déconnexion supprime aussi toute demande de sortie en attente.

## Vérification

`uv run python -m pytest tests/test_chatbot_transitions.py -q`

Les tests couvrent les transitions et, avec Streamlit AppTest, les déclarations
réelles des trois widgets, leurs callbacks et le dialogue de la page. Les écritures
sont simulées : aucun service ni aucune base ne sont contactés. Sont vérifiés les
choix du rappel, la fermeture, les cas sans rappel, les formulaires V1/V2 et la
reprise après échec d’enregistrement.
