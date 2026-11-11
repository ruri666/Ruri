"""Optional read-only public knowledge and bounded calculation capabilities.

This module receives JSON on stdin and returns actual observations on stdout.
It never invents a scene, chooses a topic, or writes canonical memory.
"""
from __future__ import annotations
import ast
from datetime import datetime, timezone
import html
import json
import math
import re
import sys
import urllib.parse
import urllib.request


def calculate(expression):
    if not isinstance(expression, str) or not 1 <= len(expression) <= 500:
        raise ValueError("Expression must contain 1..500 characters")
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 80:
        raise ValueError("Expression too complex")
    functions = {name: getattr(math, name) for name in ("sqrt", "log", "log10", "sin", "cos", "tan", "exp", "floor", "ceil")}
    functions.update(abs=abs, min=min, max=max)
    constants = {"pi": math.pi, "e": math.e}
    def bounded(value):
        if type(value) not in {int, float} or not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError("Result exceeds numeric resource bounds")
        return value
    def visit(node, depth=0):
        if depth > 16:
            raise ValueError("Expression too deep")
        if isinstance(node, ast.Constant):
            return bounded(node.value)
        if isinstance(node, ast.Name) and node.id in constants:
            return constants[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand, depth+1)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            a, b = visit(node.left, depth+1), visit(node.right, depth+1)
            if isinstance(node.op, ast.Pow) and abs(b) > 100:
                raise ValueError("Exponent too large")
            operations = {ast.Add: lambda: a+b, ast.Sub: lambda: a-b, ast.Mult: lambda: a*b,
                          ast.Div: lambda: a/b, ast.FloorDiv: lambda: a//b, ast.Mod: lambda: a%b,
                          ast.Pow: lambda: a**b}
            if type(node.op) in operations:
                return bounded(operations[type(node.op)]())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions and not node.keywords and 1 <= len(node.args) <= 8:
            return bounded(functions[node.func.id](*(visit(a, depth+1) for a in node.args)))
        raise ValueError("Unsupported expression; only numeric arithmetic and listed math functions")
    return {"expression": expression, "value": visit(tree.body), "method": "bounded numeric evaluation"}


def research(arguments):
    operation, language, query = (arguments.get(k) for k in ("operation", "language", "query"))
    if operation not in {"search", "read"} or language not in {"en", "zh"} or not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
        raise ValueError("Expected operation search/read, language en/zh and a bounded query/title")
    params = {"action": "query", "format": "json", "utf8": 1}
    if operation == "search":
        params.update(list="search", srsearch=query, srlimit=5)
    else:
        params.update(prop="extracts|info|revisions", explaintext=1, exchars=5000, inprop="url", rvprop="ids|timestamp", titles=query, redirects=1)
    endpoint = f"https://{language}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(endpoint, headers={"User-Agent": "ArtificialLifeResearch/0.4 (read-only local research)"})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(250001)
        if len(raw) > 250000:
            raise ValueError("Source response too large")
        data = json.loads(raw)
    if "error" in data:
        raise ValueError("Source API returned an error")
    result = {"operation": operation, "query": query, "language": language,
              "retrieved_at": datetime.now(timezone.utc).isoformat(), "request_url": endpoint,
              "source_kind": "public encyclopedia; a source to examine, not proof of every claim"}
    if operation == "search":
        result["items"] = [{"title": item["title"], "page_id": item["pageid"],
            "url": f"https://{language}.wikipedia.org/?curid={item['pageid']}",
            "snippet": html.unescape(re.sub("<[^>]+>", "", item.get("snippet", "")))[:800]}
            for item in data.get("query", {}).get("search", [])]
    else:
        pages = list(data.get("query", {}).get("pages", {}).values())
        if len(pages) != 1 or "missing" in pages[0] or not pages[0].get("extract"):
            raise ValueError("No readable extract for this title; search for the exact title first")
        page = pages[0]
        result.update(title=page["title"], url=page.get("fullurl"), page_id=page["pageid"],
                      revisions=page.get("revisions", []), text=page["extract"][:5000],
                      truncated=True, limitation="bounded article extract, not the entire article")
    return result


def play(board, move):
    """One real, stateless tic-tac-toe turn against a deterministic opponent."""
    wins = [(0,1,2), (3,4,5), (6,7,8), (0,3,6), (1,4,7), (2,5,8), (0,4,8), (2,4,6)]
    def won(position, mark):
        return any(all(position[i] == mark for i in line) for line in wins)
    if not isinstance(board, str) or len(board) != 9 or set(board)-set(".XO") or board.count("X") != board.count("O") or won(board, "X") or won(board, "O"):
        raise ValueError("Expected an unfinished nine-cell board with equal X/O counts; . means empty")
    if type(move) is not int or not 0 <= move <= 8 or board[move] != ".":
        raise ValueError("Choose a legal empty cell 0..8")
    position = list(board)
    position[move] = "X"
    opponent = None
    if not won(position, "X") and "." in position:
        order = [4, 0, 2, 6, 8, 1, 3, 5, 7]
        for mark in ("O", "X"):
            for index in order:
                if position[index] == ".":
                    attempt = list(position)
                    attempt[index] = mark
                    if won(attempt, mark):
                        opponent = index
                        break
            if opponent is not None:
                break
        if opponent is None:
            opponent = next(i for i in order if position[i] == ".")
        position[opponent] = "O"
    status = "won" if won(position, "X") else "lost" if won(position, "O") else "draw" if "." not in position else "ongoing"
    return {"game": "tic-tac-toe", "board_before": board, "board": "".join(position),
            "agent_symbol": "X", "agent_move": move, "opponent_move": opponent, "status": status,
            "execution": "rules engine executed these moves; no claim of using an external game app"}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"research", "calculate", "play"}:
        raise ValueError("Choose research, calculate or play")
    raw = sys.stdin.buffer.read(8193)
    if len(raw) > 8192:
        raise ValueError("Request too large")
    payload = json.loads(raw)
    if sys.argv[1] == "play":
        previous = payload.get("input")
        board = payload["arguments"]["board"]
        if previous and board != previous.get("board"):
            raise ValueError("Supplied board does not match the previous game receipt")
        result = play(board, payload["arguments"]["move"])
    else:
        result = research(payload["arguments"]) if sys.argv[1] == "research" else calculate(payload["arguments"]["expression"])
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
