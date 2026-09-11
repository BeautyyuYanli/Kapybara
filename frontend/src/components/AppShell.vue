<template>
  <a class="skip" href="#main">跳至内容</a>
  <header>
    <div class="shell">
      <RouterLink class="brand" to="/providers">Kapy · 配置</RouterLink>
      <nav aria-label="主导航">
        <RouterLink to="/providers">Provider</RouterLink><RouterLink to="/models">Model</RouterLink
        ><RouterLink to="/sessions">Session</RouterLink>
      </nav>
    </div>
  </header>
  <main id="main" class="shell" tabindex="-1"><slot /></main>
</template>
