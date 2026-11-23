/* Config-driven category controls shared by the library and advanced search. */
window.ClipperMarkingFilters = {
  create(host, onChange) {
    const controls = [];
    const result = {
      appendParams(params) {
        const selected = {};
        for (const {key, mode, values} of controls) {
          const choices = mode.value === 'values' ? [...values.selectedOptions].map(o => o.value)
            : mode.value ? [mode.value] : [];
          if (choices.length) selected[key] = choices;
        }
        if (Object.keys(selected).length) params.set('marking_filters', JSON.stringify(selected));
      }
    };
    host.textContent = 'Loading marking categories…';
    fetch('/api/marking-schema').then(async response => {
      if (!response.ok) throw new Error('Unable to load marking categories');
      return response.json();
    }).then(schema => {
      host.textContent = '';
      for (const category of schema.categories) {
        const group = document.createElement('fieldset');
        group.style.cssText = 'margin:8px 0;padding:8px;border:1px solid #ddd;font-size:12px';
        const legend = document.createElement('legend'); legend.textContent = category.label;
        const mode = document.createElement('select');
        mode.setAttribute('aria-label', category.label + ' filter mode');
        for (const [value, label] of [['','Any'],['values','Selected values'],['@present','Present'],['@missing','Missing']]) {
          mode.add(new Option(label, value));
        }
        const values = document.createElement('select'); values.multiple = true;
        values.setAttribute('aria-label', category.label + ' values');
        values.style.cssText = 'display:none;width:100%;margin-top:6px';
        values.size = Math.min(6, Math.max(2, category.values.length));
        for (const value of category.values) values.add(new Option(value, value));
        mode.onchange = () => { values.style.display = mode.value === 'values' ? 'block' : 'none'; onChange(); };
        values.onchange = onChange;
        group.append(legend, mode, values); host.append(group);
        controls.push({key:category.key, mode, values});
      }
      const hint = document.createElement('p');
      hint.textContent = 'Select several values with Ctrl/Cmd. Any selected value matches within a category; all selected categories must match.';
      host.append(hint);
    }).catch(e => { host.textContent = e.message; });
    return result;
  }
};
