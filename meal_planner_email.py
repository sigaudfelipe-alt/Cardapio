#!/usr/bin/env python3
"""
Agente de cardápio semanal (envio por e‑mail)
=============================================

Este script implementa um agente que consulta o site Panelinha para
montar um cardápio semanal de cinco dias (segunda a sexta) e enviar a
lista de compras por e‑mail.  O agente funciona em três etapas:

1. **Coleta de receitas** – A função ``get_recipe_urls`` acessa o post
   “Top 13: cardápios para resolver o jantar da semana” e extrai todas
   as URLs de receitas.  A página contém menus variados propostos pela
   Rita Lobo, e as URLs estão em links estáticos no HTML, o que permite
   extraí‑las via BeautifulSoup.

2. **Montagem do cardápio** – A função ``build_menu`` sorteia cinco
   receitas diferentes, uma para cada dia útil da semana.  Para cada
   receita, ``parse_recipe`` carrega a página individual e procura pelo
   script JSON‑LD identificado por ``js_recipe_schema`` para obter o
   nome e a lista de ingredientes.  As listas de ingredientes de todas
   as receitas selecionadas são combinadas e deduplicadas para formar
   uma lista de compras única.

3. **Envio do e‑mail** – A função ``send_email`` utiliza ``smtplib``
   para enviar uma mensagem contendo o cardápio e a lista de compras.  As
   credenciais de autenticação são lidas das variáveis de ambiente
   ``MEAL_PLANNER_EMAIL`` e ``MEAL_PLANNER_PASS``.  O destinatário é
   informado na variável ``RECIPIENT_EMAIL``.

Para usar:

```
pip install requests beautifulsoup4 schedule

# Defina as credenciais de e‑mail
export MEAL_PLANNER_EMAIL="seu-email@gmail.com"
export MEAL_PLANNER_PASS="sua-senha-ou-token"
export RECIPIENT_EMAIL="destinatario@exemplo.com"

python3 meal_planner_email.py
```

O script permanece em execução e agenda a tarefa para todo domingo às
08:00 (horário local).  No horário agendado, monta o cardápio e envia
o e‑mail correspondente.
"""

import os
import json
import random
import time
import requests
import schedule
import smtplib
from typing import List, Tuple
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# URL do blog com cardápios para o jantar da semana
BLOG_URL: str = (
    "https://panelinha.com.br/blog/ritalobo/post/top-13-cardapios-para-resolver-o-jantar-da-semana"
)


def get_recipe_urls() -> List[str]:
    """Retorna uma lista com todas as URLs de receitas encontradas no post do blog.

    A página contém vários menus, cada um com links para receitas.  Ao
    procurar todas as âncoras cujo href começa com
    ``https://www.panelinha.com.br/receita/`` ou ``/receita/``,
    coletamos esses endereços e removemos duplicidades preservando a
    ordem.
    """
    resp = requests.get(BLOG_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    anchors = soup.find_all("a", href=True)
    recipe_urls: List[str] = []
    for a in anchors:
        href = a["href"]
        if href.startswith("https://www.panelinha.com.br/receita/"):
            recipe_urls.append(href)
        elif href.startswith("/receita/"):
            recipe_urls.append(f"https://www.panelinha.com.br{href}")
    unique_urls = list(dict.fromkeys(recipe_urls))
    return unique_urls


def parse_recipe(url: str) -> Tuple[str, List[str]]:
    """Extrai o nome da receita e a lista de ingredientes da página.

    Procura pelo script JSON‑LD (``js_recipe_schema``) para obter os
    campos ``name`` e ``recipeIngredient``.  Se não estiver presente,
    tenta extrair manualmente as listas de ingredientes da página.
    """
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    title_tag = soup.find("title")
    recipe_name = title_tag.get_text(strip=True) if title_tag else url
    script_tag = soup.find("script", id="js_recipe_schema")
    ingredients: List[str] = []
    if script_tag and script_tag.string:
        try:
            data = json.loads(script_tag.string)
            recipe_name = data.get("name", recipe_name)
            ingredients = data.get("recipeIngredient", [])
        except json.JSONDecodeError:
            ingredients = []
    if not ingredients:
        # Tenta extrair manualmente os ingredientes se JSON não estiver disponível
        for h in soup.find_all(["h2", "h3", "h4", "h5"]):
            if "Ingrediente" in h.get_text():
                ul = h.find_next("ul")
                if ul:
                    for li in ul.find_all("li"):
                        text = li.get_text(strip=True)
                        if text:
                            ingredients.append(text)
        if not ingredients:
            for li in soup.find_all("li"):
                text = li.get_text(strip=True)
                if text:
                    ingredients.append(text)
    return recipe_name, ingredients


def build_menu() -> Tuple[List[Tuple[str, str]], List[str]]:
    """Sorteia cinco receitas e retorna o cardápio e a lista de compras.

    Seleciona aleatoriamente cinco URLs de receita.  Para cada uma,
    obtém o nome e os ingredientes.  Combina e deduplica os ingredientes
    (ignora diferenças de caixa) e ordena alfabeticamente.
    """
    urls = get_recipe_urls()
    if len(urls) < 5:
        raise RuntimeError(
            "Não foram encontradas receitas suficientes para montar o cardápio."
        )
    selected = random.sample(urls, 5)
    menu: List[Tuple[str, str]] = []
    all_ingredients: List[str] = []
    for url in selected:
        try:
            name, ingredients = parse_recipe(url)
        except Exception as exc:
            print(f"Falha ao processar {url}: {exc}")
            continue
        menu.append((name, url))
        all_ingredients.extend(ingredients)
    normalized = {}
    for item in all_ingredients:
        key = item.strip().lower()
        if key not in normalized:
            normalized[key] = item.strip()
    unique_ingredients = sorted(normalized.values(), key=lambda s: s.lower())
    return menu, unique_ingredients


def compose_email(menu: List[Tuple[str, str]], ingredients: List[str]) -> str:
    """Gera o corpo do e‑mail com o cardápio e a lista de compras.

    A lista de dias inclui apenas os dias úteis (segunda a sexta).  Para
    cada dia, inclui o nome da receita e o link.  Ao final, lista os
    ingredientes deduplicados.
    """
    dias_semana = [
        "Segunda-feira",
        "Terça-feira",
        "Quarta-feira",
        "Quinta-feira",
        "Sexta-feira",
    ]
    linhas: List[str] = []
    linhas.append("Olá! Aqui está o cardápio semanal sugerido:\n")
    for idx, (nome, url) in enumerate(menu):
        dia = dias_semana[idx % len(dias_semana)]
        linhas.append(f"{dia}: {nome} — {url}")
    linhas.append("\nLista de compras:")
    for item in ingredients:
        linhas.append(f"- {item}")
    return "\n".join(linhas)


def send_email(subject: str, body: str) -> None:
    """Envia um e‑mail simples utilizando SMTP com TLS.

    Lê as credenciais de envio nas variáveis de ambiente
    ``MEAL_PLANNER_EMAIL`` e ``MEAL_PLANNER_PASS`` e o destinatário em
    ``RECIPIENT_EMAIL``.  Ajuste o servidor SMTP conforme o seu provedor
    (por padrão utiliza Gmail).  Será utilizado SSL na porta 465.
    """
    user = os.environ.get("MEAL_PLANNER_EMAIL")
    password = os.environ.get("MEAL_PLANNER_PASS")
    recipient = os.environ.get("RECIPIENT_EMAIL")
    if not user or not password or not recipient:
        raise RuntimeError(
            "Credenciais ou destinatário ausentes. Configure as variáveis "
            "de ambiente MEAL_PLANNER_EMAIL, MEAL_PLANNER_PASS e RECIPIENT_EMAIL."
        )
    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    smtp_server = "smtp.gmail.com"
    smtp_port = 465
    with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
        server.login(user, password)
        server.sendmail(user, recipient, msg.as_string())


def job() -> None:
    """Tarefa agendada que monta o menu e envia o e‑mail."""
    print("Construindo cardápio...")
    menu, ingredients = build_menu()
    corpo = compose_email(menu, ingredients)
    try:
        send_email(subject="Cardápio semanal e lista de compras", body=corpo)
        print("Cardápio enviado com sucesso!")
    except Exception as exc:
        print(f"Falha ao enviar o e‑mail: {exc}")


def schedule_job() -> None:
    """Agenda a execução do job todo domingo às 08:00 (horário local)."""
    schedule.every().sunday.at("08:00").do(job)
    print("Agente de cardápio iniciado. Aguardando o horário programado...")
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    schedule_job()