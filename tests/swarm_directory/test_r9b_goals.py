"""Static copy contract: aspirations cannot be detached from today's limits."""
from html.parser import HTMLParser
from pathlib import Path
import pytest

HOME = Path(__file__).resolve().parents[2] / 'mycelium_swarm_directory/web/apex.html'

class Goals(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards = {}
        self.active = None
        self.today = False
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'article' and 'data-goal' in attrs:
            self.active = attrs['data-goal']
            self.cards[self.active] = {'text': '', 'today': '', 'links': []}
        if self.active and tag == 'p':
            self.today = 'goal-today' in attrs.get('class', '').split()
        if self.active and tag == 'a':
            self.cards[self.active]['links'].append(attrs.get('href'))
    def handle_endtag(self, tag):
        if tag == 'article': self.active = None
        if tag == 'p': self.today = False
    def handle_data(self, data):
        if self.active:
            self.cards[self.active]['text'] += data
            if self.today: self.cards[self.active]['today'] += data

def assert_goal_boundaries(html):
    parser = Goals(); parser.feed(html)
    assert set(parser.cards) == {'verifiable', 'distributed', 'private', 'edge'}
    for card in parser.cards.values():
        assert 'Goal:' in card['text'] and 'Why it matters:' in card['text']
        assert card['today'].startswith('Today:')
    assert 'not in this release' in parser.cards['verifiable']['today']
    assert 'in development' in parser.cards['verifiable']['today']
    assert 'devices can read prompts' in parser.cards['private']['today']
    assert 'not end-to-end encrypted' in parser.cards['private']['today']
    assert '#privacy-heading' in parser.cards['private']['links']
    assert 'Apple silicon Macs' in parser.cards['distributed']['today']
    assert 'other device types are in development' in parser.cards['edge']['today']

def test_goals_have_adjacent_current_release_qualifiers():
    html = HOME.read_text()
    assert 'AI on the devices you already own' in html
    assert 'a model too big for any one device' in html
    assert_goal_boundaries(html)

@pytest.mark.parametrize('goal', ['verifiable', 'private'])
def test_guard_rejects_goal_without_its_own_today_qualifier(goal):
    html = HOME.read_text()
    assert_goal_boundaries(html)
    # A disclaimer elsewhere on the page must not satisfy this local guard.
    start = html.index('data-goal="' + goal + '"')
    end = html.index('</article>', start)
    broken = html[:start] + html[start:end].replace('class="goal-today"', 'class="detached"') + html[end:]
    with pytest.raises(AssertionError):
        assert_goal_boundaries(broken)
