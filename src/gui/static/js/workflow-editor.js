(function () {
  const builder = document.querySelector("[data-workflow-builder]");
  if (!builder) return;

  const dataEl = document.getElementById("workflow-editor-data");
  let data = {};
  try {
    data = JSON.parse(dataEl ? dataEl.textContent || "{}" : "{}");
  } catch (_error) {
    data = {};
  }

  const backups = data.backups || [];
  const rcloneTasks = data.rcloneTasks || [];
  const stepList = builder.querySelector("[data-step-list]");
  const stepValue = builder.querySelector('textarea[name="steps"]');
  const emptyState = builder.querySelector("[data-empty-state]");
  const addButton = builder.querySelector("[data-add-step]");
  const template = document.getElementById("workflow-step-template");

  if (!stepList || !stepValue || !emptyState || !template) return;

  function setOptions(select, values, selected) {
    select.innerHTML = "";
    values.forEach(function (value) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      option.selected = value === selected;
      select.appendChild(option);
    });
  }

  function updateRow(row) {
    const kind = row.querySelector("[data-step-kind]").value;
    const task = row.querySelector("[data-step-task]");
    const actionWrap = row.querySelector("[data-action-wrap]");
    const action = row.querySelector("[data-step-action]");
    const initialTask = row.querySelector("[data-initial-task]");
    const values = kind === "rclone" ? rcloneTasks : backups;
    const current = task.value || (initialTask ? initialTask.value : "");

    setOptions(task, values, current);
    task.disabled = values.length === 0;
    actionWrap.hidden = kind === "rclone";
    action.disabled = kind === "rclone";
  }

  function serialize() {
    const steps = [];
    stepList.querySelectorAll("[data-step-row]").forEach(function (row, index) {
      row.querySelector("[data-step-index]").textContent = index + 1;
      const kind = row.querySelector("[data-step-kind]").value;
      const task = row.querySelector("[data-step-task]").value;
      const action = row.querySelector("[data-step-action]").value;
      row.querySelector("[data-move-up]").disabled = index === 0;
      row.querySelector("[data-move-down]").disabled = index === stepList.children.length - 1;
      if (!task) return;
      if (kind === "rclone") {
        steps.push("rclone." + task);
      } else {
        steps.push(action === "all" ? "backup." + task : "backup." + task + "." + action);
      }
    });
    stepValue.value = steps.join("\n");
    emptyState.hidden = stepList.children.length > 0;
  }

  function bindRow(row) {
    const kind = row.querySelector("[data-step-kind]");
    const action = row.querySelector("[data-step-action]");
    const initialKind = row.querySelector("[data-initial-kind]");
    const initialAction = row.querySelector("[data-initial-action]");

    if (initialKind) kind.value = initialKind.value;
    if (initialAction) action.value = initialAction.value || "all";
    updateRow(row);

    row.querySelectorAll("select").forEach(function (select) {
      select.addEventListener("change", function () {
        updateRow(row);
        serialize();
      });
    });
    row.querySelector("[data-remove-step]").addEventListener("click", function () {
      row.remove();
      serialize();
    });
    row.querySelector("[data-move-up]").addEventListener("click", function () {
      if (row.previousElementSibling) stepList.insertBefore(row, row.previousElementSibling);
      serialize();
    });
    row.querySelector("[data-move-down]").addEventListener("click", function () {
      if (row.nextElementSibling) stepList.insertBefore(row.nextElementSibling, row);
      serialize();
    });
  }

  function addStep() {
    const row = template.content.firstElementChild.cloneNode(true);
    row.querySelector("[data-step-kind]").value = backups.length > 0 ? "backup" : "rclone";
    stepList.appendChild(row);
    bindRow(row);
    serialize();
  }

  stepList.querySelectorAll("[data-step-row]").forEach(bindRow);
  serialize();
  if (addButton) addButton.addEventListener("click", addStep);
})();
