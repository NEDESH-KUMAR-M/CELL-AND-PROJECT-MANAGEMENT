document.addEventListener('DOMContentLoaded', () => {
  const sidebar = document.getElementById('sidebar');
  const toggleBtn = document.getElementById('toggleBtn');
  const mobileMenuToggle = document.getElementById('mobileMenuToggle');

  // Check for saved sidebar state
  const isSidebarCollapsed = localStorage.getItem('sidebarCollapsed') === 'true';
  if (isSidebarCollapsed) {
    sidebar.classList.add('collapsed');
  } else {
    sidebar.classList.remove('collapsed');
  }

  // Toggle sidebar on button click
  toggleBtn.addEventListener('click', () => {
    sidebar.classList.toggle('collapsed');
    const isCollapsed = sidebar.classList.contains('collapsed');
    
    // Save state to localStorage
    localStorage.setItem('sidebarCollapsed', isCollapsed);
  });

  // Mobile menu toggle
  mobileMenuToggle.addEventListener('click', () => {
    sidebar.classList.toggle('active');
  });

  // Close mobile sidebar when clicking outside
  document.addEventListener('click', (e) => {
    if (window.innerWidth <= 992 && !sidebar.contains(e.target) && !mobileMenuToggle.contains(e.target)) {
      sidebar.classList.remove('active');
    }
  });
});