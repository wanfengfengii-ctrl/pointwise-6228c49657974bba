"""Ordered captures, version 1. Unicode code-point offsets; Python 3.11 semantics."""
import re


class SyntaxError(ValueError):
    pass


class _Parser:
    def __init__(self, source):
        if not isinstance(source, str):
            raise SyntaxError('source must be text')
        self.source = source
        self.pos = 0
        self.groups = 0

    def expression(self):
        branches = [self.sequence()]
        while self.peek() == '|':
            self.pos += 1
            branches.append(self.sequence())
        return '|'.join(branches)

    def peek(self):
        return self.source[self.pos:self.pos + 1]

    def sequence(self):
        parts = []
        while self.peek() and self.peek() not in ')|':
            c = self.peek()
            self.pos += 1
            if c == '(':
                self.groups += 1
                part = '(' + self.expression()
                if self.peek() != ')':
                    raise SyntaxError('unclosed capture')
                self.pos += 1
                part += ')'
            elif c == '\\':
                if not self.peek():
                    raise SyntaxError('trailing escape')
                part = re.escape(self.peek())
                self.pos += 1
            elif c in '*?+.[\u005d{}^$':
                raise SyntaxError('unexpected operator: ' + c)
            else:
                part = re.escape(c)
            if self.peek() and self.peek() in '*?':
                part += self.peek()
                self.pos += 1
                if self.peek() and self.peek() in '*?':
                    raise SyntaxError('stacked quantifiers')
            parts.append(part)
        return ''.join(parts)

    def parse(self):
        canonical = self.expression()
        if self.pos != len(self.source):
            raise SyntaxError('unmatched closing capture')
        try:
            program = re.compile(canonical)
        except (re.error, RecursionError) as exc:
            raise SyntaxError(str(exc)) from exc
        return program, self.groups


def compile_plan(source):
    try:
        _, count = _Parser(source).parse()
    except RecursionError as exc:
        raise SyntaxError('capture nesting exceeds interpreter capacity') from exc
    return {'format': 'ordered-capture/1', 'source': source,
            'groups': [{'id': 'g' + str(n), 'number': n}
                       for n in range(1, count + 1)]}


def load(plan):
    if not isinstance(plan, dict) or plan.get('format') != 'ordered-capture/1':
        raise SyntaxError('not an SDK1 plan')
    expected = compile_plan(plan.get('source'))
    if plan != expected:
        raise SyntaxError('capture directory differs from source')
    return plan


def search(plan, text):
    load(plan)
    if not isinstance(text, str):
        raise TypeError('input line must be text')
    program, _ = _Parser(plan['source']).parse()
    match = program.search(text)
    if match is None:
        return None
    return {'span': list(match.span()),
            'captures': {g['id']: (None if match.start(g['number']) == -1
                                   else list(match.span(g['number'])))
                         for g in plan['groups']}}
