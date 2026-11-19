---
kind: qa_sheet
language: fr
---

# Fiche de révision pytest

## Question : À quoi servent les fixtures pytest ?

Une fixture prépare une ressource nécessaire à un test et la libère ensuite ; le test la reçoit en déclarant un paramètre du même nom. Les fixtures remplacent les méthodes setUp et tearDown, se composent entre elles et peuvent être partagées par un fichier conftest.py.

```python
@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
```

## Question : Comment exécuter le même test avec plusieurs jeux de données ?

Le décorateur parametrize exécute le test une fois par jeu de valeurs et nomme chaque cas, ce qui rend un échec précis. On l'emploie plutôt qu'une boucle dans le test, qui s'arrêterait au premier échec.

```python
@pytest.mark.parametrize("a, b, attendu", [(1, 2, 3), (0, 0, 0)])
def test_addition(a, b, attendu):
    assert a + b == attendu
```

## Question : Quand faut-il utiliser un mock et quand l'éviter ?

Un mock remplace une frontière que le test ne contrôle pas : réseau, horloge, système de fichiers. On l'évite pour la logique que l'on veut vérifier, sinon le test confirme seulement ce que le mock a été configuré pour répondre, et il casse à chaque refactorisation.

## Question : Comment vérifier qu'une fonction lève une exception ?

On utilise le gestionnaire de contexte pytest.raises, qui échoue si l'exception n'est pas levée, et dont on peut contrôler le message avec le paramètre match.

```python
with pytest.raises(ValueError, match="négatif"):
    racine_carree(-1)
```

## Question : Comment séparer les tests lents des tests rapides ?

On pose un marqueur personnalisé sur les tests lents, déclaré dans la configuration, puis on sélectionne ou on exclut ces tests en ligne de commande avec l'option -m. La suite rapide tourne alors à chaque modification et la suite lente en intégration continue.

## Question : Qu'est-ce que la couverture de code ne garantit pas ?

La couverture mesure les lignes exécutées, pas les comportements vérifiés. Un test sans assertion fait monter le pourcentage sans rien protéger ; seule une mutation du code qui fait échouer un test prouve que ce test a un pouvoir discriminant.
