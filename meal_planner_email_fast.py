#!/usr/bin/env python3
"""
Agente de cardápio semanal otimizado (envio por e‑mail)
=======================================================

Esta versão do agente monta um cardápio de cinco dias (segunda a
sexta) e envia a lista de compras por e‑mail, com otimizações para
reduzir o tempo de execução.  As principais melhorias são:

* **Cache da lista de receitas** – o script salva as URLs de receitas
  extraídas da página do Panelinha em ``receitas_cache.json``.  Em
  execuções subsequentes, evita baixar a página se o cache existir.

* **Paralelização da leitura de receitas** – utiliza
  ``concurrent.futures.ThreadPoolExecutor`` para baixar e parsear
  várias receitas simultaneamente, aproveitando melhor o tempo de
  espera das requisições.

O envio do e‑mail continua dependente das variáveis de ambiente
``MEAL_PLANNER_EMAIL``, ``MEAL_PLANNER_PASS`` e ``RECIPIENT_EMAIL``.  O
agendamento permanece aos domingos às 08h00 (horário local).
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
from concurrent.futures import ThreadPoolExecutor, as_completed

# URL do blog com cardápios para o jantar da semana
BLOG_URL: str = (
    "https://panelinha.com.br/blog/ritalobo/post/top-13-cardapios-para-resolver-o-jantar-da-semana"
)


def get_recipe_urls() -> List[str]:
    """Obtém a lista de URLs de receitas, usando cache se disponível.

    Se o arquivo ``receitas_cache.json`` existir no diretório atual,
    carrega as URLs desse arquivo.  Caso contrário, baixa a página do
    blog, extrai os links das receitas e grava o cache para futuras
    execuções.
    """
    cache_path = "receitas_cache.json"
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                urls = json.load(f)
            if isinstance(urls, list) and urls:
                return urls
        except Exception:
            pass  # se houver erro de leitura, continua e baixa do site
    # Baixa a página e extrai URLs
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
    # Salva o cache
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(unique_urls, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return unique_urls


def parse_recipe(url: str) -> Tuple[str, List[str]]:
    """Extrai o nome da receita e a lista de ingredientes da página.

    Esta função é idêntica à usada na versão padrão, buscando primeiro o
    script JSON‑LD (``js_recipe_schema``) e, se necessário, extraindo
    manualmente os ingredientes do HTML.
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

    Utiliza um pool de threads para acelerar a obtenção e o parsing das
    receitas selecionadas.  Deduplica os ingredientes ignorando caixa.
    """
    urls = get_recipe_urls()
    if len(urls) < 5:
        raise RuntimeError(
            "Não foram encontradas receitas suficientes para montar o cardápio."
        )
    selected = random.sample(urls, 5)
    menu: List[Tuple[str, str]] = []
    all_ingredients: List[str] = []
    # Processa as receitas em paralelo
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_url = {executor.submit(parse_recipe, url): url for url in selected}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                name, ingredients = future.result()
                menu.append((name, url))
                all_ingredients.extend(ingredients)
            except Exception as exc:
                print(f"Falha ao processar {url}: {exc}")
    # Deduplicação de ingredientes
    normalized = {}
    for item in all_ingredients:
        key = item.strip().lower()
        if key not in normalized:
            normalized[key] = item.strip()
    unique_ingredients = sorted(normalized.values(), key=lambda s: s.lower())
    return menu, unique_ingredients


def compose_email(menu: List[Tuple[str, str]], ingredients: List[str]) -> str:
    """Gera o corpo do e‑mail com o cardápio e a lista de compras.

    Inclui cinco dias úteis e lista de compras deduplicada.
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
    """Envia um e‑mail simples utilizando SMTP com TLS."""
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