
(function(setWorkflowTabsRenderer){
  function setupSlidingPill() {
    const tabsList = document.getElementById('workflowTabsList');
    if (!tabsList) return;
    let pill = tabsList.querySelector('.wf-tab-sliding-pill');
    if (!pill) {
      pill = document.createElement('div');
      pill.className = 'wf-tab-sliding-pill';
      tabsList.prepend(pill);
    }
    const activeItem = tabsList.querySelector('.wf-tab.active');
    if (activeItem) {
      const parentRect = tabsList.getBoundingClientRect();
      const tabRect = activeItem.getBoundingClientRect();
      pill.style.transform = `translateX(${tabRect.left - parentRect.left + tabsList.scrollLeft}px)`;
      pill.style.width = `${tabRect.width}px`;
      pill.style.display = 'block';
    } else {
      pill.style.display = 'none';
    }
  }

  const origRenderTabs = window.renderWorkflowTabs;
  if (typeof origRenderTabs === 'function') {
    window.renderWorkflowTabs = function() {
      origRenderTabs.apply(this, arguments);
      setTimeout(setupSlidingPill, 10);
    };
    setWorkflowTabsRenderer(window.renderWorkflowTabs);
  }

  function setupSidebarResizer() {
    const sidebar = document.getElementById('sidebar');
    if (!sidebar) return;
    let handle = sidebar.querySelector('.sidebar-resizer-handle');
    if (!handle) {
      handle = document.createElement('div');
      handle.className = 'sidebar-resizer-handle';
      handle.title = '按住拖动调整宽度，双击复位';
      sidebar.appendChild(handle);
    }
    let isDragging = false, startX = 0, startW = 280;
    const savedW = localStorage.getItem('haflow_custom_sidebar_w');
    if (savedW) sidebar.style.width = savedW + 'px';

    handle.addEventListener('mousedown', (e) => {
      isDragging = true;
      startX = e.clientX;
      startW = sidebar.getBoundingClientRect().width;
      handle.classList.add('is-dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    });
    window.addEventListener('mousemove', (e) => {
      if (!isDragging) return;
      const newW = Math.max(220, Math.min(460, startW + (e.clientX - startX)));
      sidebar.style.width = newW + 'px';
    });
    window.addEventListener('mouseup', () => {
      if (!isDragging) return;
      isDragging = false;
      handle.classList.remove('is-dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      localStorage.setItem('haflow_custom_sidebar_w', sidebar.getBoundingClientRect().width);
    });
    handle.addEventListener('dblclick', () => {
      sidebar.style.width = '280px';
      localStorage.setItem('haflow_custom_sidebar_w', '280');
    });
  }

  window.addEventListener('DOMContentLoaded', () => {
    setupSidebarResizer();
    setupSlidingPill();
  });
})(setWorkflowTabsRenderer);
