---
kind: qa_sheet
language: fr
---

# Fiche de révision FastAPI

## Question : Qu'est-ce que Depends dans FastAPI ?

### Réponse

Depends déclare une dépendance que FastAPI résout et injecte à chaque requête.

```python
from fastapi import Depends, FastAPI

app = FastAPI()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/items")
def read_items(db=Depends(get_db)):
    return db.query(Item).all()
```

### Explication

FastAPI appelle get_db avant l'endpoint, passe la session obtenue en argument, puis exécute la fin du générateur une fois la réponse envoyée.

## Question : Comment tester un endpoint qui dépend d'une base de données ?

On remplace la dépendance par une version de test grâce au dictionnaire dependency_overrides de l'application. Le test n'ouvre alors aucune connexion réelle et reste rapide, déterministe et isolé des autres tests de la suite.

Cette approche évite de patcher les modules internes : l'endpoint reçoit exactement ce que la dépendance de test lui fournit, comme en production, et le contrat entre la route et sa dépendance reste vérifié par le framework lui-même.

Il faut penser à vider dependency_overrides à la fin de chaque test, sinon le remplacement fuit vers les tests suivants et produit des échecs difficiles à reproduire, qui dépendent de l'ordre d'exécution de la suite, voire de la machine sur laquelle les tests tournent ce jour-là.

## Question : Quelle différence entre async def et def pour un endpoint ?

Un endpoint déclaré avec async def s'exécute directement dans la boucle d'événements ; un endpoint déclaré avec def est exécuté dans un pool de threads afin de ne pas bloquer la boucle.

```python
@app.get("/lent")
async def lent():
    await asyncio.sleep(1)
    return {"ok": True}
```
