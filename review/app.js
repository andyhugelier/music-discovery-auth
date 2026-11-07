'use strict';

const $ = id => document.getElementById(id);
let profile, csrf, selectedGroup = null, excluded = false, offset = 0, searchTimer, renaming, editingTrack, requestSequence = 0, busy = false;
const count = n => new Intl.NumberFormat().format(n);
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function button(text, className, handler) {
  const element = node('button', className, text);
  element.type = 'button';
  element.addEventListener('click', handler);
  return element;
}
async function api(path, body) {
  const response = await fetch(path, body ? {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Review-CSRF': csrf}, body: JSON.stringify(body)} : {cache: 'no-store'});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'The profile could not be loaded. Please retry.');
  return data;
}
function status(message, error = false) {
  $('status').textContent = message;
  $('status').className = error ? 'error' : '';
  $('status').hidden = !message;
}
function renderProfile() {
  $('stat-tracks').textContent = count(profile.source_tracks);
  $('stat-groups').textContent = profile.groups.length;
  $('stat-excluded').textContent = count(profile.excluded_source_tracks);
  $('nav-count').textContent = count(profile.excluded_source_tracks);
  $('coverage-note').textContent = `${count(profile.grouped_source_tracks)} eligible source tracks have a group; ${count(profile.unclassified_source_tracks)} remain unclassified. ${profile.artist_enrichment.complete || 0} artists have cached community tags. Unclassified does not mean disliked.`;
  $('groups').replaceChildren();
  profile.groups.forEach((group, index) => {
    const card = node('article', `group-card color-${group.id}`);
    const art = node('div', 'group-art');
    art.setAttribute('aria-hidden', 'true');
    art.append(node('span', 'art-index', String(index + 1).padStart(2, '0')), node('div', 'art-shape'), node('span', 'art-caption', 'A THREAD, NOT A BOX'));
    const content = node('div', 'group-content');
    const heading = button(group.name, 'group-title', () => selectGroup(group));
    const description = node('p', 'group-description', group.examples.map(t => t.artist).join(' · ') || 'Explore the evidence behind this group.');
    const bottom = node('div', 'group-bottom');
    bottom.append(node('span', '', `${count(group.tracks)} source tracks`), button('Rename', 'text-button', () => {
      renaming = group.id;
      $('group-name').value = group.name;
      $('rename-dialog').showModal();
      $('group-name').focus();
    }));
    content.append(heading, description, bottom);
    card.append(art, content);
    $('groups').append(card);
  });
}
function evidence(t) {
  const s = t.signals;
  if (t.source === 'apple') return s.negative ? 'Explicit negative rating' : s.positive ? 'Favorited / positively rated · counted once' : 'Saved in library · weak preference evidence';
  if (t.source === 'lastfm') return `${s.loved ? 'Loved · ' : ''}${count(s.scrobbles)} historical scrobbles${s.undated_scrobbles ? ` · ${count(s.undated_scrobbles)} undated` : ''}`;
  return `${count(s.at_least_30s)} streams of 30s+ · ${count(s.streams)} source rows · skips are not dislikes`;
}
async function loadTracks(append = false) {
  const sequence = ++requestSequence;
  if (!append) {offset = 0; $('track-list').replaceChildren();}
  $('load-more').disabled = true;
  $('track-list').setAttribute('aria-busy', 'true');
  const params = new URLSearchParams({excluded: excluded ? '1' : '0', offset: String(offset), q: $('search').value});
  if (selectedGroup && !excluded) params.set('group', selectedGroup.id);
  try {
    const data = await api(`/api/tracks?${params}`);
    if (sequence !== requestSequence) return;
    $('result-count').textContent = `${count(data.total)} source tracks`;
    data.items.forEach(t => {
      const row = node('article', 'track-row');
      const monogram = node('div', `track-monogram source-${t.source}`, t.artist.slice(0, 1).toUpperCase());
      monogram.setAttribute('aria-hidden', 'true');
      const description = node('div', 'track-description');
      description.append(node('h3', '', t.title), node('p', 'artist-line', `${t.artist}${t.album ? ` · ${t.album}` : ''}`), node('p', 'evidence', t.excluded ? t.exclusion_reason : evidence(t)));
      if (t.basis && !t.excluded) description.append(node('p', 'basis', t.basis));
      const controls = node('div', 'track-controls');
      controls.append(node('span', 'source-badge', t.source === 'lastfm' ? 'Last.fm' : t.source));
      if (excluded) {
        if (t.exclusion_reason === 'Explicit negative song rating' || t.exclusion_reason === 'Private-session or zero-playback evidence only') {
          controls.append(node('span', 'small-note', 'Source evidence'));
        } else {
          controls.append(button('Include artist', 'secondary small', () => review('keep_artist', t.artist_key, '', `Included ${t.artist}.`)));
        }
      } else {
        controls.append(button('Edit groups', 'text-button', () => {
          editingTrack = t.id;
          $('editing-track').textContent = `${t.title} · ${t.artist}`;
          $('group-choices').replaceChildren();
          profile.group_options.forEach(group => {
            const label = node('label');
            const input = node('input'); input.type = 'checkbox'; input.value = group.id;
            input.checked = t.group_ids.includes(group.id);
            label.append(input, document.createTextNode(group.name));
            $('group-choices').append(label);
          });
          $('groups-dialog').showModal();
        }), button('Exclude track', 'text-button', () => review('exclude_track', t.id, '', 'Track excluded from taste.')),
          button('Exclude artist', 'text-button', () => review('exclude_artist', t.artist_key, '', `Excluded ${t.artist}.`)));
      }
      row.append(monogram, description, controls);
      $('track-list').append(row);
    });
    if (!data.items.length && !append) $('track-list').append(node('p', 'empty', 'No tracks match this view. Try another search or group.'));
    offset += data.items.length;
    $('load-more').hidden = offset >= data.total;
    status('');
  } catch (error) { if (sequence === requestSequence) status(error.message, true); }
  finally { if (sequence === requestSequence) {$('load-more').disabled = false; $('track-list').setAttribute('aria-busy', 'false');} }
}
async function review(kind, target, value, message) {
  if (busy) return;
  busy = true;
  document.body.classList.add('saving');
  status('Saving your review…');
  try {
    const data = await api('/api/actions', {kind, target, value});
    profile = data.profile;
    renderProfile();
    if (selectedGroup) selectedGroup = profile.groups.find(g => g.id === selectedGroup.id) || null;
    $('track-heading').textContent = excluded ? 'Separate contexts & exclusions' : selectedGroup ? selectedGroup.name : 'Tracks & evidence';
    if (!$('track-section').hidden) await loadTracks();
    $('toast-text').textContent = message;
    $('toast').hidden = false;
    $('undo').hidden = !profile.last_action;
    status('');
  } catch (error) {status(error.message, true);}
  finally {busy = false; document.body.classList.remove('saving');}
}
function setView(showExcluded) {
  excluded = showExcluded;
  selectedGroup = null;
  $('search').value = '';
  $('taste-view').hidden = excluded;
  $('track-section').hidden = !excluded;
  $('page-title').replaceChildren(document.createTextNode(excluded ? 'A place for' : 'Many sides.'), node('br'), document.createTextNode(excluded ? 'everything else.' : 'All your taste.'));
  $('intro-copy').textContent = excluded ? 'Family listening, background sounds and your exclusions stay separate. Your original history is preserved.' : 'Explore the threads running through your listening. Keep what feels like you, and give everything else its own space.';
  $('nav-taste').classList.toggle('active', !excluded);
  $('nav-excluded').classList.toggle('active', excluded);
  $('nav-taste').setAttribute('aria-current', excluded ? 'false' : 'page');
  $('nav-excluded').setAttribute('aria-current', excluded ? 'page' : 'false');
  $('track-heading').textContent = excluded ? 'Separate contexts & exclusions' : 'Tracks & evidence';
  $('clear-group').hidden = excluded;
  if (excluded) loadTracks();
}
function selectGroup(group) {
  selectedGroup = group;
  $('search').value = '';
  $('track-section').hidden = false;
  $('track-heading').textContent = group.name;
  loadTracks();
  $('track-section').scrollIntoView({behavior: 'smooth', block: 'start'});
}
$('nav-taste').addEventListener('click', () => setView(false));
$('nav-excluded').addEventListener('click', () => setView(true));
$('browse-all').addEventListener('click', () => {setView(false); $('track-section').hidden = false; loadTracks(); $('track-section').scrollIntoView({behavior: 'smooth'});});
$('clear-group').addEventListener('click', () => {selectedGroup = null; $('track-heading').textContent = 'Tracks & evidence'; loadTracks();});
$('load-more').addEventListener('click', () => loadTracks(true));
$('search').addEventListener('input', () => {clearTimeout(searchTimer); searchTimer = setTimeout(() => loadTracks(), 250);});
$('cancel-rename').addEventListener('click', () => $('rename-dialog').close());
$('cancel-groups').addEventListener('click', () => $('groups-dialog').close());
$('groups-form').addEventListener('submit', event => {
  event.preventDefault();
  const ids = [...document.querySelectorAll('#group-choices input:checked')].map(input => input.value);
  $('groups-dialog').close(); review('set_groups', editingTrack, JSON.stringify(ids), 'Track groups saved.');
});
$('rename-form').addEventListener('submit', event => {event.preventDefault(); $('rename-dialog').close(); review('rename_group', renaming, $('group-name').value, 'Group name saved.');});
$('undo').addEventListener('click', () => {if (profile.last_action) review('undo', String(profile.last_action.id), '', 'Review undone.');});
api('/api/profile').then(data => {profile = data.profile; csrf = data.csrf; renderProfile(); status(profile.built_utc ? '' : 'Build your profile to begin.');}).catch(error => status(error.message, true));
