/**
 * VaPls Stremio & Anime Web App - Frontend Logic with HTML Escaping Security
 */

document.addEventListener('DOMContentLoaded', () => {
  // Security Helper: HTML Escaping to prevent XSS attacks
  function esc(str) {
    if (str === null || str === undefined) return '';
    const d = document.createElement('div');
    d.textContent = String(str);
    return d.innerHTML;
  }

  // DOM Elements
  const searchInput = document.getElementById('searchInput');
  const clearSearch = document.getElementById('clearSearch');
  const catalogGrid = document.getElementById('catalogGrid');
  const emptyState = document.getElementById('emptyState');
  const loader = document.getElementById('loader');
  const tabBtns = document.querySelectorAll('.tab-btn');
  const chipBtns = document.querySelectorAll('.chip-btn');

  // Voice Status & Picker
  const voiceStatusText = document.getElementById('voiceStatusText');
  const voiceChannelSelect = document.getElementById('voiceChannelSelect');

  // Modal Elements
  const detailModal = document.getElementById('detailModal');
  const closeModal = document.getElementById('closeModal');
  const modalBanner = document.getElementById('modalBanner');
  const modalPoster = document.getElementById('modalPoster');
  const modalTitle = document.getElementById('modalTitle');
  const modalTypeBadge = document.getElementById('modalTypeBadge');
  const modalYear = document.getElementById('modalYear');
  const modalImdb = document.getElementById('modalImdb');
  const modalGenres = document.getElementById('modalGenres');
  const modalDescription = document.getElementById('modalDescription');

  const episodeSection = document.getElementById('episodeSection');
  const seasonSelect = document.getElementById('seasonSelect');
  const episodeSelect = document.getElementById('episodeSelect');
  const episodeTitlePreview = document.getElementById('episodeTitle');

  const streamLoader = document.getElementById('streamLoader');
  const streamList = document.getElementById('streamList');
  const refreshStreamsBtn = document.getElementById('refreshStreamsBtn');
  const startStreamBtn = document.getElementById('startStreamBtn');

  const toast = document.getElementById('toast');
  const toastMessage = document.getElementById('toastMessage');

  // Application State
  let currentFilter = 'all';
  let searchTimeout = null;
  let currentMeta = null;
  let selectedStreamUrl = null;
  let voiceChannels = [];

  // Init
  fetchVoiceChannels();

  // Event Listeners
  searchInput.addEventListener('input', (e) => {
    const val = e.target.value.trim();
    clearSearch.style.display = val ? 'block' : 'none';
    if (searchTimeout) clearTimeout(searchTimeout);
    
    if (!val) {
      showEmptyState();
      return;
    }

    searchTimeout = setTimeout(() => {
      performSearch(val, currentFilter);
    }, 350);
  });

  clearSearch.addEventListener('click', () => {
    searchInput.value = '';
    clearSearch.style.display = 'none';
    showEmptyState();
  });

  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      tabBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentFilter = btn.dataset.type;
      
      const query = searchInput.value.trim();
      if (query) {
        performSearch(query, currentFilter);
      }
    });
  });

  chipBtns.forEach(chip => {
    chip.addEventListener('click', () => {
      searchInput.value = chip.textContent.trim();
      clearSearch.style.display = 'block';
      performSearch(searchInput.value, currentFilter);
    });
  });

  closeModal.addEventListener('click', hideModal);
  detailModal.addEventListener('click', (e) => {
    if (e.target === detailModal) hideModal();
  });

  seasonSelect.addEventListener('change', updateEpisodeOptions);
  episodeSelect.addEventListener('change', () => {
    if (currentMeta) fetchStreams();
  });

  refreshStreamsBtn.addEventListener('click', () => {
    if (currentMeta) fetchStreams();
  });

  startStreamBtn.addEventListener('click', triggerDiscordStream);

  // Helper Functions
  function showEmptyState() {
    catalogGrid.style.display = 'none';
    loader.style.display = 'none';
    emptyState.style.display = 'block';
  }

  function showLoader() {
    emptyState.style.display = 'none';
    catalogGrid.style.display = 'none';
    loader.style.display = 'block';
  }

  function showCatalog() {
    loader.style.display = 'none';
    emptyState.style.display = 'none';
    catalogGrid.style.display = 'grid';
  }

  async function fetchVoiceChannels() {
    try {
      const resp = await fetch('/api/stremio/voice-channels');
      if (!resp.ok) throw new Error('Network error');
      const data = await resp.json();
      voiceChannels = data.channels || [];
      renderVoiceChannelPicker();
    } catch (e) {
      voiceStatusText.textContent = 'Servidor local activo';
      voiceChannelSelect.innerHTML = '<option value="">No se encontraron canales de voz activos</option>';
    }
  }

  function renderVoiceChannelPicker() {
    const voiceStatusPill = document.getElementById('voiceStatusPill');

    if (!voiceChannels.length) {
      voiceStatusText.textContent = 'Sin usuarios en voz';
      if (voiceStatusPill) voiceStatusPill.setAttribute('title', 'No hay usuarios conectados en voz');
      voiceChannelSelect.innerHTML = '<option value="">Sin canal de voz activo</option>';
      return;
    }

    const fullTooltipParts = [];
    const pillParts = [];

    voiceChannels.forEach(ch => {
      const members = ch.members || [];
      const memberCount = ch.members_count || members.length;
      const allMembersStr = members.join(', ');
      fullTooltipParts.push(`${ch.name} (${memberCount}): ${allMembersStr || 'Sin nombres'}`);

      let truncatedMembers = '';
      if (members.length <= 3) {
        truncatedMembers = members.join(', ');
      } else {
        truncatedMembers = `${members.slice(0, 2).join(', ')} +${members.length - 2} más`;
      }
      pillParts.push(`${ch.name}: ${truncatedMembers}`);
    });

    const pillDisplay = pillParts.join(' | ');
    const fullTooltip = fullTooltipParts.join('\n');

    voiceStatusText.textContent = pillDisplay;
    if (voiceStatusPill) {
      voiceStatusPill.setAttribute('title', fullTooltip);
    }

    voiceChannelSelect.innerHTML = voiceChannels.map(ch => {
      const members = ch.members || [];
      const allMembersStr = members.join(', ');
      let displayMembers = '';
      if (members.length <= 3) {
        displayMembers = members.join(', ');
      } else {
        displayMembers = `${members.slice(0, 2).join(', ')} +${members.length - 2} más`;
      }
      const optionLabel = `🔊 ${ch.guild_name ? ch.guild_name + ' → ' : ''}${ch.name}${displayMembers ? ' (' + displayMembers + ')' : ''}`;
      return `
        <option value="${esc(ch.id)}" data-guild="${esc(ch.guild_id)}" title="${esc(ch.name)}: ${esc(allMembersStr)}">
          ${esc(optionLabel)}
        </option>
      `;
    }).join('');
  }

  async function performSearch(query, filter) {
    showLoader();
    try {
      const resp = await fetch(`/api/stremio/search?q=${encodeURIComponent(query)}&type=${encodeURIComponent(filter)}`);
      if (resp.status === 429) {
        showToast('⚠️ Demasiadas peticiones. Aguardá unos segundos.');
        showEmptyState();
        return;
      }
      if (!resp.ok) throw new Error('Search failed');
      const results = await resp.json();

      if (!results.length) {
        catalogGrid.innerHTML = `
          <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-muted);">
            No se encontraron resultados para "${esc(query)}". Probá buscar otro título.
          </div>
        `;
        showCatalog();
        return;
      }

      renderCatalogGrid(results);
      showCatalog();
    } catch (e) {
      catalogGrid.innerHTML = `
        <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--accent-pink);">
          Ocurrió un error al buscar. Intente nuevamente.
        </div>
      `;
      showCatalog();
    }
  }

  function renderCatalogGrid(items) {
    catalogGrid.innerHTML = items.map(item => `
      <div class="card" data-id="${esc(item.id)}" data-type="${esc(item.type)}">
        <img class="card-poster" src="${esc(item.poster || 'https://via.placeholder.com/300x450?text=No+Poster')}" alt="${esc(item.title)}" loading="lazy">
        <div class="card-content">
          <div class="card-title">${esc(item.title)}</div>
          <div class="card-meta">
            <span class="badge ${esc(item.type)}">${esc(item.type.toUpperCase())}</span>
            <span>${esc(item.year || '')}</span>
          </div>
        </div>
      </div>
    `).join('');

    catalogGrid.querySelectorAll('.card').forEach(card => {
      card.addEventListener('click', () => {
        const id = card.dataset.id;
        const type = card.dataset.type;
        openDetailModal(id, type);
      });
    });
  }

  async function openDetailModal(id, type) {
    detailModal.style.display = 'flex';
    selectedStreamUrl = null;
    startStreamBtn.disabled = true;
    streamList.innerHTML = '';
    streamLoader.style.display = 'block';

    modalTitle.textContent = 'Cargando detalles...';
    modalGenres.textContent = '';
    modalDescription.textContent = '';
    episodeSection.style.display = 'none';

    try {
      const resp = await fetch(`/api/stremio/meta?id=${encodeURIComponent(id)}&type=${encodeURIComponent(type)}`);
      if (!resp.ok) throw new Error('Failed to fetch meta');
      currentMeta = await resp.json();

      modalTitle.textContent = currentMeta.title || '';
      modalPoster.src = currentMeta.poster || 'https://via.placeholder.com/300x450?text=No+Poster';
      
      if (currentMeta.banner) {
        modalBanner.style.backgroundImage = `url('${esc(currentMeta.banner)}')`;
      } else {
        modalBanner.style.backgroundImage = 'none';
      }

      modalTypeBadge.textContent = (currentMeta.type || '').toUpperCase();
      modalTypeBadge.className = `badge ${esc(currentMeta.type)}`;
      modalYear.textContent = currentMeta.year || '';
      modalGenres.textContent = (currentMeta.genres || []).join(' • ');
      modalDescription.textContent = currentMeta.description || 'Sin descripción disponible.';

      if (currentMeta.episodes && currentMeta.episodes.length > 0) {
        setupEpisodePicker(currentMeta.episodes);
        episodeSection.style.display = 'block';
      } else {
        episodeSection.style.display = 'none';
        await fetchStreams();
      }
    } catch (e) {
      modalTitle.textContent = 'Error al cargar detalles';
      streamLoader.style.display = 'none';
    }
  }

  function setupEpisodePicker(episodes) {
    const seasonsMap = {};
    episodes.forEach(ep => {
      const s = ep.season || 1;
      if (!seasonsMap[s]) seasonsMap[s] = [];
      seasonsMap[s].push(ep);
    });

    seasonSelect.innerHTML = Object.keys(seasonsMap).map(s => `<option value="${esc(s)}">Temporada ${esc(s)}</option>`).join('');
    updateEpisodeOptions();
  }

  function updateEpisodeOptions() {
    if (!currentMeta || !currentMeta.episodes) return;
    const selectedSeason = parseInt(seasonSelect.value) || 1;
    const filteredEps = currentMeta.episodes.filter(e => (e.season || 1) === selectedSeason);

    episodeSelect.innerHTML = filteredEps.map(e => `
      <option value="${esc(e.episode || 1)}">Episodio ${esc(e.episode || 1)} - ${esc(e.title)}</option>
    `).join('');

    if (filteredEps.length > 0) {
      episodeTitlePreview.textContent = filteredEps[0].overview || filteredEps[0].title;
    }

    fetchStreams();
  }

  async function fetchStreams() {
    if (!currentMeta) return;
    streamLoader.style.display = 'block';
    streamList.innerHTML = '';
    startStreamBtn.disabled = true;
    selectedStreamUrl = null;

    const season = seasonSelect.value ? parseInt(seasonSelect.value) : 1;
    const episode = episodeSelect.value ? parseInt(episodeSelect.value) : 1;

    let streams = [];

    try {
      const url = `/api/stremio/streams?id=${encodeURIComponent(currentMeta.id)}&type=${encodeURIComponent(currentMeta.type)}&season=${season}&episode=${episode}&imdb_id=${encodeURIComponent(currentMeta.imdb_id || '')}`;
      const resp = await fetch(url);
      if (resp.ok) {
        streams = await resp.json();
      }
    } catch (e) {}

    // Fallback: If server returned no streams, fetch directly from client browser
    if (!streams || !streams.length) {
      try {
        const debrid = 'torbox=90f73123-7565-4ae3-b672-aa96bc026c50';
        const targetId = currentMeta.imdb_id || (currentMeta.id && currentMeta.id.startsWith('tt') ? currentMeta.id : null);
        let clientUrl = '';
        if (targetId) {
          clientUrl = currentMeta.type === 'movie'
            ? `https://torrentio.strem.fun/${debrid}/stream/movie/${targetId}.json`
            : `https://torrentio.strem.fun/${debrid}/stream/series/${targetId}:${season}:${episode}.json`;
        } else if (currentMeta.id && currentMeta.id.startsWith('kitsu:')) {
          clientUrl = `https://torrentio.strem.fun/${debrid}/stream/series/${currentMeta.id}:${episode}.json`;
        }

        if (clientUrl) {
          const clientResp = await fetch(clientUrl);
          if (clientResp.ok) {
            const cdata = await clientResp.json();
            const rawStreams = cdata.streams || [];
            streams = rawStreams.map(s => {
              const directUrl = s.url || '';
              const infohash = s.infoHash || '';
              const titleRaw = s.title || s.name || 'Torrent Stream';
              const lines = titleRaw.split('\n').map(l => l.trim()).filter(Boolean);
              const mainTitle = lines[0] || 'Torrent Stream';
              const magnet = infohash ? `magnet:?xt=urn:btih:${infohash}&dn=${encodeURIComponent(mainTitle)}` : '';
              return {
                name: s.name || 'Torrentio',
                title: mainTitle,
                quality: mainTitle.includes('2160P') || mainTitle.includes('4K') ? '4K' : (mainTitle.includes('1080P') ? '1080p' : 'HD'),
                seeders: -1,
                size: '',
                details: lines.slice(1).join(' '),
                url: directUrl || magnet,
                infohash: infohash || 'torbox',
                is_direct: directUrl.includes('torrentio.strem.fun/resolve/torbox/') || directUrl.includes('tb-cdn')
              };
            });
          }
        }
      } catch (err) {}
    }

    streamLoader.style.display = 'none';

    if (!streams || !streams.length) {
      streamList.innerHTML = '<div style="padding: 12px; color: var(--text-muted);">No se encontraron enlaces de streaming para esta opción.</div>';
      return;
    }

    renderStreams(streams);
  }

  function renderStreams(streams) {
    streamList.innerHTML = streams.map((s, idx) => `
      <div class="stream-item" data-url="${esc(s.url)}" data-title="${esc(s.title)}">
        <div class="stream-info">
          <div class="stream-name">
            ${s.is_direct ? '<span class="direct-tag">⚡ Stream Directo</span>' : ''}
            ${esc(s.title)}
          </div>
          <div class="stream-meta">
            ${s.quality ? `[${esc(s.quality)}]` : ''} ${s.seeders >= 0 ? `👤 ${esc(s.seeders)}` : ''} ${s.size ? `💾 ${esc(s.size)}` : ''} ${esc(s.details || '')}
          </div>
        </div>
      </div>
    `).join('');

    const items = streamList.querySelectorAll('.stream-item');
    items.forEach((item, idx) => {
      item.addEventListener('click', () => {
        items.forEach(i => i.classList.remove('selected'));
        item.classList.add('selected');
        selectedStreamUrl = item.dataset.url;
        startStreamBtn.disabled = false;
      });

      if (idx === 0) {
        item.click();
      }
    });
  }

  async function triggerDiscordStream() {
    if (!selectedStreamUrl) return;

    const channelOpt = voiceChannelSelect.selectedOptions[0];
    const channelId = voiceChannelSelect.value;
    const guildId = channelOpt ? channelOpt.dataset.guild : null;

    if (!channelId) {
      showToast('⚠️ Por favor seleccioná un canal de voz de Discord primero.');
      return;
    }

    startStreamBtn.disabled = true;
    startStreamBtn.textContent = '⏳ Conectando Go Live...';

    try {
      const payload = {
        url: selectedStreamUrl,
        title: currentMeta ? currentMeta.title : 'Stream Stremio',
        channel_id: channelId,
        guild_id: guildId,
      };

      const resp = await fetch('/api/stremio/play', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      const res = await resp.json();
      if (resp.ok && res.status === 'ok') {
        showToast('🚀 ¡Transmisión Go Live iniciada en Discord!');
        hideModal();
      } else {
        showToast(`❌ Error: ${esc(res.error || 'No se pudo iniciar el stream')}`);
      }
    } catch (e) {
      showToast('❌ Error de conexión al servidor del bot.');
    } finally {
      startStreamBtn.disabled = false;
      startStreamBtn.innerHTML = '<span class="play-icon">▶</span> Transmitir en Discord Go Live';
    }
  }

  function hideModal() {
    detailModal.style.display = 'none';
  }

  function showToast(msg) {
    toastMessage.textContent = msg;
    toast.style.display = 'flex';
    setTimeout(() => {
      toast.style.display = 'none';
    }, 4500);
  }
});
