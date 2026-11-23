# Fiche de révision Python

## GIL

Le Global Interpreter Lock est un verrou de CPython qui n'autorise qu'un seul thread à exécuter du bytecode à la fois.

## Décorateur

Un décorateur est une fonction qui prend une fonction et en renvoie une autre, généralement pour ajouter un comportement avant ou après son appel.

### Exemple

```python
def chronometre(fonction):
    def enveloppe(*args, **kwargs):
        return fonction(*args, **kwargs)
    return enveloppe
```

## Générateur

Un générateur est une fonction qui contient yield et produit ses valeurs paresseusement, une à la fois, en conservant son état entre deux appels.
