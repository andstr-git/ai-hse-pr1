# repo-map

Индекс Python-репозитория на Tree-sitter: определения, imports, вызовы и пять запросов к приближенному call graph. Разработанный CLI-инструмент `repo-map` позволяет агенту спрашивать "где определен символ", "кто вызывает функцию", "есть ли путь вызовов" – вместо чтения всего репозитория.

## Запуск

```bash
uv sync
uv run repo-map --help
```

Для примеров ниже нужен клон FastAPI рядом с решением:

```bash
git clone https://github.com/fastapi/fastapi.git ../fastapi
git -C ../fastapi checkout 95f8322
```

## Запросы

```bash
uv run repo-map definition fastapi.FastAPI --root ../fastapi/fastapi
uv run repo-map references fastapi.FastAPI --root ../fastapi/fastapi
uv run repo-map callers fastapi.openapi.utils.get_openapi --root ../fastapi/fastapi
uv run repo-map callees fastapi.applications.FastAPI.openapi --root ../fastapi/fastapi
uv run repo-map path fastapi.applications.FastAPI.openapi fastapi.openapi.utils.get_openapi_path --root ../fastapi/fastapi
```

`definition fastapi.FastAPI` ищет определение имени `fastapi.FastAPI`. В исходнике в `fastapi/__init__.py` написан только импорт библиотеки `from .applications import FastAPI`, а настоящий класс описан в другом файле (`fastapi/applications.py`), который и позволяет найти команда.

`references fastapi.FastAPI` находит все места, где на символ так или иначе ссылаются (например, импорты, вызовы функций: явные `FastAPI()` или через super-классы и декораторы)

`callers fastapi.openapi.utils.get_openapi` отвечает "кто вызывает функцию": возвращает список символов, внутри которых обнаружен разрешенный вызов. Полезно проверить перед изменением сигнатуры функции.

`callees fastapi.applications.FastAPI.openapi` показывает, из чего состоит функция: внутренние вызовы (здесь – `fastapi.openapi.utils.get_openapi`), внешние (`external`) и неразрешенные (`unresolved`) с причиной, почему разбор не смог определить цель.

`path fastapi.applications.FastAPI.openapi fastapi.openapi.utils.get_openapi_path` ищет кратчайший путь вызовов между двумя символами: `openapi -> get_openapi -> get_openapi_path`; если пути нет, отвечает `path: null`.

## Формат JSON

Выводится один JSON-объект в stdout с полями как в условии. Дополнения: в `references` кроме `import` и `call` может выводиться `other` – использования в списке super-классов и в декораторах. В `callees` причины unresolved те же, что в условии: `local variable`, `dynamic receiver`, `not defined in class`, `unknown name`.

## Проверки

```bash
uv run pytest -q                 # тесты на fixture tiny_pkg и tests/fixtures/rules_pkg
uv run pytest -q -m fastapi      # публичные smoke tests, нужен ../fastapi
uv run ruff check .
uv run ruff format --check .
```
