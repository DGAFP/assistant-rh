PYTHON ?= python3

.PHONY: help dev dump dev-down dev-logs dev-rebuild
help:
	@echo 'make dev       Lancer Conversations et l’API RH sur localhost'
	@echo 'make dump      Copier les corpus/config de staging dans la base locale dédiée'
	@echo 'make dev-logs  Suivre les journaux'
	@echo 'make dev-down  Arrêter la stack en conservant les données'
	@echo 'make dev-rebuild Reconstruire les images et actualiser les dépendances'

dev dump dev-down dev-logs dev-rebuild:
	@$(PYTHON) scripts/dev.py $@
