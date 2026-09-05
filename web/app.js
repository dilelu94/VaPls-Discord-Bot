/**
 * VaPls Stremio & Anime Web App - Frontend Logic
 */

document.addEventListener('DOMContentLoaded', () => {
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
    if (!voiceChannels.length) {
      voiceStatusText.textContent = 'Sin usuarios en voz';
      voiceChannelSelect.innerHTML = '<option value="">Sin canal de voz activo</option>';
      return;
    }

    voiceStatusText.textContent = `${voiceChannels.length} canal(es) de voz disponible(s)`;
    voiceChannelSelect.innerHTML = voiceChannels.map(ch => `
      <option value="${ch.id}" data-guild="${ch.guild_id}">
        🔊 ${ch.guild_name ? ch.guild_name + ' -> ' : ''}${ch.name} (${ch.members_count} miembros)
      </option>
    `).join('');
  }

  async function performSearch(query, filter) {
    showLoader();
    try {
      const resp = await fetch(`/api/stremio/search?q=${encodeURIComponent(query)}&type=${filter}`);
      if (!resp.ok) throw new Error('Search failed');
      const results = await resp.json();

      if (!results.length) {
        catalogGrid.innerHTML = `
          <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-muted);">
            No se encontraron resultados para "${query}". Probá buscar otro título.
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
      <div class="card" data-id="${item.id}" data-type="${item.type}">
        <img class="card-poster" src="${item.poster || 'https://via.placeholder.com/300x450?text=No+Poster'}" alt="${item.title}" loading="lazy">
        <div class="card-content">
          <div class="card-title">${item.title}</div>
          <div class="card-meta">
            <span class="badge ${item.type}">${item.type.toUpperCase()}</span>
            <span>${item.year || ''}</span>
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
      const resp = await fetch(`/api/stremio/meta?id=${encodeURIComponent(id)}&type=${type}`);
      if (!resp.ok) throw new Error('Failed to fetch meta');
      currentMeta = await resp.json();

      modalTitle.textContent = currentMeta.title;
      modalPoster.src = currentMeta.poster || 'https://via.placeholder.com/300x450?text=No+Poster';
      
      if (currentMeta.banner) {
        modalBanner.style.backgroundImage = `url('${currentMeta.banner}')`;
      } else {
        modalBanner.style.backgroundImage = 'none';
      }

      modalTypeBadge.textContent = currentMeta.type.toUpperCase();
      modalTypeBadge.className = `badge ${currentMeta.type}`;
      modalYear.textContent = currentMeta.year || '';
      modalGenres.textContent = (currentMeta.genres || []).join(' • ');
      modalDescription.textContent = currentMeta.description || 'Sin descripción disponible.';

      // Handle Episodes
      if (currentMeta.episodes && currentMeta.episodes.length > 0) {
        setupEpisodePicker(currentMeta.episodes);
        episodeSection.style.display = 'block';
      } else {
        episodeSection.style.display = 'none';
      }

      await fetchStreams();
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

    seasonSelect.innerHTML = Object.keys(seasonsMap).map(s => `<option value="${s}">Temporada ${s}</option>`).join('');
    updateEpisodeOptions();
  }

  function updateEpisodeOptions() {
    if (!currentMeta || !currentMeta.episodes) return;
    const selectedSeason = parseInt(seasonSelect.value) || 1;
    const filteredEps = currentMeta.episodes.filter(e => (e.season || 1) === selectedSeason);

    episodeSelect.innerHTML = filteredEps.map(e => `
      <option value="${e.episode || 1}">Episodio ${e.episode || 1} - ${e.title}</option>
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

    try {
      const url = `/api/stremio/streams?id=${encodeURIComponent(currentMeta.id)}&type=${currentMeta.type}&season=${season}&episode=${episode}&imdb_id=${encodeURIComponent(currentMeta.imdb_id || '')}`;
      const resp = await fetch(url);
      if (!resp.ok) throw new Error('Failed to fetch streams');
      const streams = await resp.json();

      streamLoader.style.display = 'none';

      if (!streams || !streams.length) {
        streamList.innerHTML = '<div style="padding: 12px; color: var(--text-muted);">No se encontraron enlaces de streaming para esta opción.</div>';
        return;
      }

      renderStreams(streams);
    } catch (e) {
      streamLoader.style.display = 'none';
      streamList.innerHTML = '<div style="padding: 12px; color: var(--accent-pink);">Error al obtener streams.</div>';
    }
  }

  function renderStreams(streams) {
    streamList.innerHTML = streams.map((s, idx) => `
      <div class="stream-item" data-url="${s.url}" data-title="${s.title}">
        <div class="stream-info">
          <div class="stream-name">
            ${s.is_direct ? '<span class="direct-tag">⚡ TorBox Directo</span>' : ''}
            ${s.title}
          </div>
          <div class="stream-meta">
            ${s.quality ? `[${s.quality}]` : ''} ${s.seeders >= 0 ? `👤 ${s.seeders}` : ''} ${s.size ? `💾 ${s.size}` : ''} ${s.details || ''}
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

      // Auto-select first stream (especially if direct TorBox)
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
        showToast(`❌ Error: ${res.error || 'No se pudo iniciar el stream'}`);
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
