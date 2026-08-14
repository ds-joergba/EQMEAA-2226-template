.ONESHELL:

schema_tempdir:=$(shell mktemp -d)
DIFF_TARGET := $(if $(filter Deploy,$(TRIGGER_TYPE)),HEAD~1,HEAD)
GITHUB_REPO_NAME := $(basename $(notdir $(SCHEMA_URL)))
ENVS := prj-eqmeaa-dev-agkt

# TODO activate those ENVS again prj-eqmeaa-staging-glgc prj-eqmeaa-prod-yate

clone_and_checkout_repo:
	@echo "STEP: Cloning schema repository"
	@git clone $(SCHEMA_URL) $(schema_tempdir)
ifneq ($(BRANCH),)
	@echo "Checkout branch $(BRANCH)"
	@cd "$(schema_tempdir)" && git checkout $(BRANCH)
endif
	(
		cd $(schema_tempdir)
# 		git clone "$(LOOKER_URL)" ./looker
		mkdir ./looker
		git clone "$(APIS_URL)" ./apis
	)


validate_yamls: clone_and_checkout_repo
	@echo -e "\nSTEP: Validating YAML files"
	yaml_validation_report="$$(python3 yaml_validator.py --json_schema=$(JSON_SCHEMA) --schema_repo_dir=$(schema_tempdir)/$(REPO_DIR))"
	if [ -z "$$yaml_validation_report" ]; then
		python3 report_artifacts.py --content="" --comment="YAML files are valid" --commit_sha=${COMMIT_SHA} --pull_request_id=${PR_ID} --project_name=$(GITHUB_OWNER) --git_repo_name=$(GITHUB_REPO_NAME) --api_key=${HTTP_API_KEY} --state=success --context="ci/yaml-validation"
	else
		@echo "ERROR: Found invalid YAML files"
		@echo $$yaml_validation_report
		python3 report_artifacts.py --content="$$yaml_validation_report" --comment="Invalid YAML files detected" --commit_sha=${COMMIT_SHA} --pull_request_id=${PR_ID} --project_name=$(GITHUB_OWNER) --git_repo_name=$(GITHUB_REPO_NAME) --api_key=${HTTP_API_KEY} --state=failure --context="ci/yaml-validation"
		@exit 1
	fi


check_deleted_yamls: validate_yamls
	@(
		cd "$(schema_tempdir)/$(REPO_DIR)"
		git diff main...HEAD --name-only --diff-filter=D -- '*.yaml' > deleted_yamls.txt
	)
	@echo -e "\nSTEP: Check for deleted YAML files"
	@if [ ! -s $(schema_tempdir)/$(REPO_DIR)/deleted_yamls.txt ]; then
		python3 report_artifacts.py --content="" --comment="No YAML files have been deleted" --commit_sha=${COMMIT_SHA} --pull_request_id=${PR_ID} --project_name=$(GITHUB_OWNER) --git_repo_name=$(GITHUB_REPO_NAME) --api_key=${HTTP_API_KEY} --state=success --context="ci/yaml-deletion-check"
	@else
		@echo "ERROR: Found deleted YAML files"
		@cat $(schema_tempdir)/$(REPO_DIR)/deleted_yamls.txt
		python3 report_artifacts.py --content="Not allowed to delete $$deleted_yamls" --comment="Deleted YAML files detected" --commit_sha=${COMMIT_SHA} --pull_request_id=${PR_ID} --project_name=$(GITHUB_OWNER) --git_repo_name=$(GITHUB_REPO_NAME) --api_key=${HTTP_API_KEY} --state=failure --context="ci/yaml-deletion-check"
		@exit 1
	@fi

prepare_schemas: check_deleted_yamls
	@echo -e "\nSTEP: Prepare Schemas"
	@(
		cd "$(schema_tempdir)"
		git diff --name-only $(DIFF_TARGET) origin/main ./$(REPO_DIR)/*.yaml | sed "s|^|$(schema_tempdir)/|" > $(REPO_DIR)/changed_yaml_files.txt
	)
	@(
		cd "$(schema_tempdir)/apis"
		git ls-files $(APIS_ROOT_DIR)/$(REPO_DIR)/*.proto | sed "s|^|$(schema_tempdir)/apis/|" > $(schema_tempdir)/$(REPO_DIR)/original_proto_files.txt
	)

	@echo -e "\nChanged YAML files: $$(cat $(schema_tempdir)/$(REPO_DIR)/changed_yaml_files.txt)"
	@echo -e "\nProto Files on main: $$(cat $(schema_tempdir)/$(REPO_DIR)/original_proto_files.txt)"

	@mkdir -p $(schema_tempdir)/apis/$(APIS_ROOT_DIR)/$(REPO_DIR)
	python3 yaml_converter.py --yaml_list=$(schema_tempdir)/$(REPO_DIR)/changed_yaml_files.txt --original_proto_names=$(schema_tempdir)/$(REPO_DIR)/original_proto_files.txt --proto_dir=$(schema_tempdir)/apis/$(APIS_ROOT_DIR)/$(REPO_DIR) --git_repo_name=$(REPO_DIR) --report=$(schema_tempdir)

	@(
		cd "$(schema_tempdir)/apis"
		git ls-files -o --exclude-standard $(APIS_ROOT_DIR)/$(REPO_DIR)/*.proto | sed "s|^|$(schema_tempdir)/apis/|" > $(schema_tempdir)/$(REPO_DIR)/new_proto_files.txt
		echo -e "\nNew Proto Files: $$(cat $(schema_tempdir)/$(REPO_DIR)/new_proto_files.txt)"
	)

	@cp looker/*.lookml $(schema_tempdir)/looker/
	@echo -e "\nStarting Schema Register in prepare schemas"
	python3 schema_register.py --git_repo_name=$(REPO_DIR) --gcp_project=${GCP_PROJECT} --dry_run=True --output_path=$(schema_tempdir) --yaml_list=$(schema_tempdir)/$(REPO_DIR)/changed_yaml_files.txt --proto_list=$(schema_tempdir)/$(REPO_DIR)/new_proto_files.txt --schema_dataset=${SCHEMA_DATASET} --raw_dataset=${RAW_DATASET} --raw2_dataset=${RAW2_DATASET} --looker_root=$(schema_tempdir)/looker --execute_looker=${EXECUTE_LOOKER}

	(
		cd $(schema_tempdir)/looker
		echo -e "\nLooker diff: $$(git diff --name-only)"
	)

	if [ $(wc -l < $(schema_tempdir)/$(REPO_DIR)/changed_yaml_files.txt) -eq $(wc -l < $(schema_tempdir)/yaml-to-proto-report.txt) ]; then
		echo "INFO: Successfully prepared schemas"
	else
		echo "ERROR: Schema preparation failed"
#		set PR to change requested
		@exit 1
	fi

prepare_deployment: prepare_schemas
	@echo -e "\nSTEP: Prepare Deployment"
	@(
		cd "$(schema_tempdir)"
		git config --global user.email "root@dwh-schema-registry.com"
		git config --global user.name "root"
		git ls-files --deleted | xargs git add
		git diff --quiet && git diff --staged --quiet || git commit -am 'CI/CD update main [skip ci]'
	)
	@(
		cd "$(schema_tempdir)/apis"
		git config --global user.email "root@dwh-schema-registry.com"
		git config --global user.name "root"
		git add $(APIS_ROOT_DIR)/$(REPO_DIR)/*
		git diff --quiet && git diff --staged --quiet || git commit -am 'CI/CD update main [skip ci]'
	)

# Case 1: Schema Change is valid, approved and merged to main
deploy: prepare_deployment
	@for ENV in $(ENVS); do \
		echo -e "\nSTEP: Deploy $$ENV"; \
		if [ "$$ENV" = "prj-eqmeaa-prod-yate" ]; then \
			python3 schema_register.py \
				--git_repo_name=$(REPO_DIR) \
				--gcp_project=$$ENV \
				--output_path=$(schema_tempdir) \
				--yaml_list=$(schema_tempdir)/$(REPO_DIR)/changed_yaml_files.txt \
				--proto_list=$(schema_tempdir)/$(REPO_DIR)/new_proto_files.txt \
				--schema_dataset=${SCHEMA_DATASET} \
				--raw_dataset=${RAW_DATASET} \
				--raw2_dataset=${RAW2_DATASET} \
				--looker_root=$(schema_tempdir)/looker \
				--execute_looker='True' \
				--looker_ini_path=${LOOKER_INI_PATH} \
				--dashboard_folder_id=${DASHBOARD_FOLDER_ID} \
				--trigger_topic=${TRIGGER_TOPIC}; \
		else \
			python3 schema_register.py \
				--git_repo_name=$(REPO_DIR) \
				--gcp_project=$$ENV \
				--output_path=$(schema_tempdir) \
				--yaml_list=$(schema_tempdir)/$(REPO_DIR)/changed_yaml_files.txt \
				--proto_list=$(schema_tempdir)/$(REPO_DIR)/new_proto_files.txt \
				--schema_dataset=${SCHEMA_DATASET} \
				--raw_dataset=${RAW_DATASET} \
				--raw2_dataset=${RAW2_DATASET} \
				--looker_root=$(schema_tempdir)/looker \
				--execute_looker=${EXECUTE_LOOKER} \
				--trigger_topic=${TRIGGER_TOPIC}; \
		fi; \
	done

	@echo "Push to Schema Repo"
	@(
		cd "$(schema_tempdir)"
		git push
	)

	@echo "Push to APIs Repo"
	@(
		cd "$(schema_tempdir)/apis"
		git push
	)

# 	@echo "Push to Looker Repo"
# 	(
# 		cd "$(schema_tempdir)/looker"
# 		git config --global user.email "root@dwh-schema-registry.com"
# 		git config --global user.name "root"
# 		git status
# 		git add *.lkml
# 		git commit -m "CI/CD update main"
# 		git push
# 	)

# 	@echo "Deploy Looker Repo"
# 	curl -i -f -H X-Looker-Deploy-Secret:$(LOOKER_DEPLOY_TOKEN) $(LOOKER_WEBHOOK_URL)

	rm -r $(schema_tempdir)

# case 2: push in open PR
pr_change:
	@$(MAKE) prepare_schemas

# case 3: run local without any changes
local_run: prepare_schemas
	@echo -e "\nSTEP: Local Run Results:"
	@echo "changed yaml files: $(schema_tempdir)/$(REPO_DIR)/changed_yaml_files.txt"
	@echo "deleted yaml files: $(schema_tempdir)/$(REPO_DIR)/deleted_yamls.txt"
	@echo "yaml convertion report: $(schema_tempdir)/yaml-to-proto-report.txt"
	@echo "new proto file names: $(schema_tempdir)/$(REPO_DIR)/new_proto_files.txt"
	@echo "damp schemas table entry: $(schema_tempdir)/bigquery_schema.txt"
