"""Owner-directed copy boundaries: broad vision, precise current requirements."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]/'mycelium_swarm_directory/web'
TODAY='Today: Apple silicon Macs; Linux, Windows, Android and iOS are in development'

def test_current_platforms_are_explicit_only_where_action_requires_them():
    app=(ROOT/'index.html').read_text()
    assert app.count(TODAY)==2
    for section in ['panel-contributing','panel-new']:
        part=app.split('id="'+section+'"',1)[1].split('</section>',1)[0]
        assert part.count(TODAY)==1
        assert 'at least two devices' in part
    home=(ROOT/'apex.html').read_text()
    # Current support is now also adjacent to the aspirations, never hidden.
    assert home.count('Apple silicon Macs') == 2
    outside = home.split('<div class="goal-grid">')[0] + home.split('<div class="goal-grid">', 1)[1].split('</div></section>', 1)[1]
    assert 'Macs' not in outside
    assert 'at least two devices' in home

def test_vision_is_direction_not_current_platform_or_privacy_promise():
    home=(ROOT/'apex.html').read_text()
    assert 'Our vision' in home and 'Our goal' in home
    assert 'larger models' in home and 'stronger privacy' in home
    assert 'This is the first step' in home
    assert 'not end-to-end encrypted' in home and 'Do not send secrets' in home
    assert 'Cloudflare' in home and 'plaintext traffic' in home
    assert 'phone book' not in home and 'phonebook' not in home
    assert 'MVP' not in home
    assert home.index('Our vision')<home.index('id="right-now"')
